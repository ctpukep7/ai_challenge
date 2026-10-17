"""Day31 retrieval scopes, independent of the shared index implementation."""


def search_sources(index, query, paths, *, day=None, scope='all'):
    if scope not in {'all', 'project'} or (scope == 'project' and day is not None):
        raise ValueError('scope=project нельзя сочетать с day; используйте scope=all с номером дня')
    docs = [path for path in paths if path.startswith('docs/')]
    if day is not None:
        anchors = index.search(query, 3, .35, [f'day-{day:02d}/README.md']) + index.search(query, 2, .35, docs)
    elif scope == 'project':
        # Reserve relevant results from both source families; never pad a weak
        # match below the existing similarity threshold to fake diversity.
        root = index.search(query, 5, .35, ['README.md'])
        documentation = index.search(query, 5, .35, docs)
        anchors = root[:1] + documentation[:1]
        seen = {item['id'] for item in anchors}
        rest = sorted(root[1:] + documentation[1:], key=lambda item: item['score'], reverse=True)
        for item in rest:
            if len(anchors) == 5:
                break
            if item['id'] not in seen:
                anchors.append(item)
                seen.add(item['id'])
        anchors.sort(key=lambda item: item['score'], reverse=True)
    else:
        anchors = index.search(query)
    return index.expand_neighbors(anchors)
