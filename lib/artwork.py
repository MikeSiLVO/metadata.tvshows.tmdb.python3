# SPDX-License-Identifier: GPL-3.0-or-later

"""Artwork classification, scoring, and byte-aware selection.

Images are classified by art type, ranked by language then pixels,
and limited to byte budgets (c06/c11) to enforce MySQL TEXT limit.
"""

_IMG_ORIGINAL = 'https://image.tmdb.org/t/p/original'
_IMG_W500 = 'https://image.tmdb.org/t/p/w500'
_IMG_W780 = 'https://image.tmdb.org/t/p/w780'

# MySQL TEXT = 65,535 bytes; 5K margin for safety
_C06_BUDGET = 60000
_C11_BUDGET = 60000
_C11_WRAPPER = 17  # <fanart></fanart>

_SPARSE_THRESHOLD = 3
_TEXT_BEARING = frozenset(('poster', 'landscape', 'clearlogo', 'banner', 'clearart'))
_LANG_TIER_WEIGHT = 10 ** 15


def set_artwork(li, show_info, settings):
    """Main entry: classify, score, select, output to ListItem."""
    vtag = li.getVideoInfoTag()
    user_lang = settings['lang_images'][:2].lower()
    cat_kart = settings.get('cat_keyart', True)
    cat_land = settings.get('cat_landscape', True)

    # Build flat list of all candidate images with metadata
    candidates = []
    _classify_images(
        candidates, show_info.get('images', {}),
        'show', None, user_lang, cat_kart, cat_land,
    )
    for season in show_info.get('seasons', []):
        snum = season.get('season_number', 0)
        simages = season.get('images')
        if simages:
            _classify_images(
                candidates, simages,
                snum, snum, user_lang, cat_kart, cat_land,
            )

    # Score all candidates: language tier primary for text-bearing types,
    # then pixel area with vote count as tiebreaker
    for c in candidates:
        base = _score(c)
        if c['art_type'] == 'clearlogo':
            pixels = min((c.get('width') or 0) * (c.get('height') or 0), 800 * 310)
            base = pixels * 1000 + (c.get('vote_count') or 0)
            if c.get('url', '').startswith('https://assets.fanart.tv/'):
                base += 10 ** 9
        if c['art_type'] in _TEXT_BEARING:
            tier = _lang_sort_key(c.get('language'), user_lang)[0]
            base += (4 - tier) * _LANG_TIER_WEIGHT
        c['score'] = base

    # Select within byte budgets
    c06 = [c for c in candidates if c['column'] == 'c06']
    c11 = [c for c in candidates if c['column'] == 'c11']
    keep_c06 = _select(c06, _C06_BUDGET)
    keep_c11 = _select(c11, _C11_BUDGET - _C11_WRAPPER)

    # Output
    fanart_urls = []
    for c in keep_c11:
        fanart_urls.append({'image': c['url']})
    if fanart_urls:
        try:
            vtag.setAvailableFanart(fanart_urls)
        except AttributeError:
            li.setAvailableFanart(fanart_urls)

    for c in keep_c06:
        kwargs = {'arttype': c['art_type'], 'preview': c['preview']}
        if c['season'] is not None:
            kwargs['season'] = c['season']
        vtag.addAvailableArtwork(c['url'], **kwargs)


def _classify_images(candidates, images, bucket_key, season, user_lang,
                     cat_kart, cat_land):
    """Classify images and append to candidates list."""
    start = len(candidates)

    for raw in images.get('posters', []):
        entry = _make_entry(raw, _IMG_W500)
        if not entry:
            continue
        lang = raw.get('iso_639_1')
        if (lang is None or lang == 'xx') and cat_kart:
            entry.update(art_type='keyart', column='c06', season=season,
                         bucket=(bucket_key, 'keyart'))
        else:
            entry.update(art_type='poster', column='c06', season=season,
                         bucket=(bucket_key, 'poster'))
        candidates.append(entry)

    for raw in images.get('backdrops', []):
        entry = _make_entry(raw, _IMG_W780)
        if not entry:
            continue
        lang = raw.get('iso_639_1')
        if lang and lang != 'xx' and cat_land:
            entry.update(art_type='landscape', column='c06', season=season,
                         bucket=(bucket_key, 'landscape'))
        else:
            entry.update(art_type='fanart', column='c11', season=season,
                         bucket=(bucket_key, 'fanart'))
        candidates.append(entry)

    for raw in images.get('logos', []):
        entry = _make_entry(raw, _IMG_W500)
        if not entry:
            continue
        entry.update(art_type='clearlogo', column='c06', season=season,
                     bucket=(bucket_key, 'clearlogo'))
        candidates.append(entry)

    for art_type in ('banner', 'clearart', 'characterart'):
        for raw in images.get(art_type, []):
            entry = _make_entry(raw, _IMG_W500)
            if not entry:
                continue
            entry.update(art_type=art_type, column='c06', season=season,
                         bucket=(bucket_key, art_type))
            candidates.append(entry)

    for raw in images.get('landscape', []):
        entry = _make_entry(raw, _IMG_W780)
        if not entry:
            continue
        entry.update(art_type='landscape', column='c06', season=season,
                     bucket=(bucket_key, 'landscape'))
        candidates.append(entry)

    # Sort language-sensitive buckets in-place (only the slice we just added)
    for art in ('poster', 'landscape', 'clearlogo', 'banner', 'clearart'):
        bk = (bucket_key, art)
        _sort_bucket_slice(candidates, start, bk, user_lang)


def _select(entries, byte_budget):
    """Sparse-first, then greedy fill by score within byte budget."""
    if not entries:
        return []

    buckets = {}
    for entry in entries:
        buckets.setdefault(entry['bucket'], []).append(entry)

    selected = []
    overflow = []
    for bucket_entries in buckets.values():
        if len(bucket_entries) <= _SPARSE_THRESHOLD:
            bucket_entries.sort(key=lambda e: e['score'], reverse=True)
            selected.extend(bucket_entries)
        else:
            overflow.extend(bucket_entries)

    used = sum(_byte_cost(e) for e in selected)

    # If sparse alone exceeds budget, trim by score
    if used > byte_budget:
        selected.sort(key=lambda e: e['score'], reverse=True)
        kept = []
        used = 0
        for e in selected:
            cost = _byte_cost(e)
            if used + cost <= byte_budget:
                kept.append(e)
                used += cost
        return kept

    # Fill remaining by score
    overflow.sort(key=lambda e: e['score'], reverse=True)
    for e in overflow:
        cost = _byte_cost(e)
        if used + cost <= byte_budget:
            selected.append(e)
            used += cost

    return selected


def _byte_cost(entry):
    """XML serialization cost for one image entry."""
    if entry['column'] == 'c11':
        # <thumb colors="" preview="">URL</thumb>
        # setAvailableFanart only receives {'image': url}, Kodi stores preview=""
        return 36 + len(entry['url'])
    # <thumb spoof="" cache="" [season="N" type="season" ]aspect="TYPE" preview="PREVIEW">URL</thumb>
    cost = 54 + len(entry['art_type']) + len(entry['preview']) + len(entry['url'])
    if entry['season'] is not None:
        cost += 24 + len(str(entry['season']))
    return cost


def _score(entry):
    """Pixel area with vote count as tiebreaker."""
    w = entry.get('width') or 0
    h = entry.get('height') or 0
    vc = entry.get('vote_count') or 0
    return w * h * 1000 + vc


def _make_entry(raw_image, preview_base):
    """Convert a raw image dict into a candidate entry."""
    path = raw_image.get('file_path')
    if not path or path.endswith('.svg'):
        return None
    if raw_image.get('type') == 'fanarttv':
        url = path
        preview = path.replace('/fanart/', '/preview/', 1)
    else:
        url = '{}{}'.format(_IMG_ORIGINAL, path)
        preview = '{}{}'.format(preview_base, path)
    return {
        'url': url,
        'preview': preview,
        'language': raw_image.get('iso_639_1'),
        'vote_count': raw_image.get('vote_count') or 0,
        'width': raw_image.get('width') or 0,
        'height': raw_image.get('height') or 0,
    }


def _sort_bucket_slice(candidates, start, bucket_key, user_lang):
    """Sort entries within a bucket by language, scanning only from start."""
    indices = [i for i in range(start, len(candidates))
               if candidates[i].get('bucket') == bucket_key]
    if not indices:
        return
    subset = [candidates[i] for i in indices]
    subset.sort(key=lambda e: _lang_sort_key(e.get('language'), user_lang))
    for idx, i in enumerate(indices):
        candidates[i] = subset[idx]


def _lang_sort_key(lang, user_lang):
    if lang:
        lang = lang.lower()
    if lang == user_lang:
        return (0,)
    if lang == 'en' and user_lang != 'en':
        return (1,)
    if not lang or lang == 'xx':
        return (2,)
    return (3, lang or '')
