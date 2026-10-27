"""One-to-one diagnostic assignment, explicitly marking equally good mappings."""

from .formats import FIELDS, identity


def _hungarian(cost):
    size = len(cost)
    u, v, p, way = [[0] * (size + 1) for _ in range(4)]
    for row in range(1, size + 1):
        p[0] = row
        column, minima, used = 0, [10**9] * (size + 1), [False] * (size + 1)
        while True:
            used[column] = True
            current, delta, following = p[column], 10**9, 0
            for target in range(1, size + 1):
                if not used[target]:
                    reduced = cost[current - 1][target - 1] - u[current] - v[target]
                    if reduced < minima[target]:
                        minima[target], way[target] = reduced, column
                    if minima[target] < delta:
                        delta, following = minima[target], target
            for target in range(size + 1):
                if used[target]:
                    u[p[target]] += delta
                    v[target] -= delta
                else:
                    minima[target] -= delta
            column = following
            if p[column] == 0:
                break
        while True:
            previous = way[column]
            p[column] = p[previous]
            column = previous
            if not column:
                break
    assignment = {p[column] - 1: column - 1 for column in range(1, size + 1)}
    # An alternating zero-reduced-cost cycle identifies another optimal mapping.
    reachable = [[False] * size for _ in range(size)]
    for row in range(size):
        for column in range(size):
            target = p[column + 1] - 1
            if target != row and cost[row][column] - u[row + 1] - v[column + 1] == 0:
                reachable[row][target] = True
    for middle in range(size):
        for row in range(size):
            for target in range(size):
                reachable[row][target] |= reachable[row][middle] and reachable[middle][target]
    ambiguous = {row for row in range(size) if any(target != row and reachable[row][target] and reachable[target][row] for target in range(size))}
    return assignment, ambiguous


def align(expected, accepted):
    remaining_expected, remaining_actual = list(range(len(expected))), list(range(len(accepted)))
    pairs = []
    # Lock exact complete identities before diagnostic matching, so unordered
    # dual picks and a partially missing second pick never displace a true match.
    for gold_index in list(remaining_expected):
        actual_index = next((index for index in remaining_actual if identity(expected[gold_index]) == identity(accepted[index])), None)
        if actual_index is not None:
            pairs.append({'expected': gold_index, 'actual': actual_index, 'exact': True, 'ambiguous': False})
            remaining_expected.remove(gold_index)
            remaining_actual.remove(actual_index)
    size = max(len(remaining_expected), len(remaining_actual))
    if size:
        cost = [[sum(expected[remaining_expected[row]][field] != accepted[remaining_actual[column]][field] for field in FIELDS)
                 if row < len(remaining_expected) and column < len(remaining_actual) else len(FIELDS) + 1
                 for column in range(size)] for row in range(size)]
        assignment, ambiguous = _hungarian(cost)
        for row, gold_index in enumerate(remaining_expected):
            column = assignment[row]
            pairs.append({'expected': gold_index, 'actual': remaining_actual[column] if column < len(remaining_actual) else None,
                          'exact': False, 'ambiguous': row in ambiguous})
    return sorted(pairs, key=lambda item: item['expected'])
