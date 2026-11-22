"""P04: pure-Python Dixon-Coles baseline over public Football-Data results.

Attack, defence, home advantage and the low-score correction (``rho``) are fit by
penalised maximum likelihood with a normalisation constraint so the multiplicative
scale cannot run away. The fit is deterministic, reports iterations and the final
log-likelihood, and refuses to hide a non-convergence. Prediction returns a normalised
scoreline grid; 1X2 and totals are summed from it and never recomputed separately, so
the probabilities always add up to one.

Training and evaluation are split by a fixed season boundary: a match's result is only
visible to the fit that trains on seasons strictly before it, so no test result leaks
back into the parameters. Public closing odds are used on the evaluation side only and
never enter the fit.
"""

from collections import Counter, defaultdict
from math import exp, lgamma, log

from .evidence import INSUFFICIENT, MEASURED, section
from .public_results import load_directory

MAX_GOALS = 10
RHO_BOUND = 0.18


def _poisson_pmf(rate, goals):
    return exp(-rate) * rate ** goals / exp(lgamma(goals + 1))


def _tau(home_goals, away_goals, home_rate, away_rate, rho):
    """Dixon-Coles low-score correction and its log derivatives.

    Returns ``(tau, d/dln_home, d/dln_away, d/drho)`` clamped so ``tau`` stays
    strictly positive; the caller rejects a parameter vector that would break it.
    """
    if home_goals == 0 and away_goals == 0:
        base = 1 - home_rate * away_rate * rho
        if base <= 0:
            return None
        return base, -home_rate * away_rate * rho / base, -home_rate * away_rate * rho / base, -home_rate * away_rate / base
    if home_goals == 0 and away_goals == 1:
        base = 1 + home_rate * rho
        if base <= 0:
            return None
        return base, home_rate * rho / base, 0.0, home_rate / base
    if home_goals == 1 and away_goals == 0:
        base = 1 + away_rate * rho
        if base <= 0:
            return None
        return base, 0.0, away_rate * rho / base, away_rate / base
    if home_goals == 1 and away_goals == 1:
        base = 1 - rho
        if base <= 0:
            return None
        return base, 0.0, 0.0, -1 / base
    return 1.0, 0.0, 0.0, 0.0


class Fit:
    """A converged (or explicitly reported) Dixon-Coles parameter set."""

    def __init__(self, attack, defence, home, rho, *, log_likelihood, iterations, converged, teams):
        self.attack = attack
        self.defence = defence
        self.home = home
        self.rho = rho
        self.log_likelihood = log_likelihood
        self.iterations = iterations
        self.converged = converged
        self.teams = teams

    def rates(self, home_team, away_team):
        home_rate = exp(self.attack[home_team] + self.defence[away_team] + self.home)
        away_rate = exp(self.attack[away_team] + self.defence[home_team])
        return home_rate, away_rate

    def grid(self, home_team, away_team, max_goals=MAX_GOALS):
        home_rate, away_rate = self.rates(home_team, away_team)
        grid = {}
        total = 0.0
        for home_goals in range(max_goals + 1):
            for away_goals in range(max_goals + 1):
                probability = _poisson_pmf(home_rate, home_goals) * _poisson_pmf(away_rate, away_goals)
                tau = _tau(home_goals, away_goals, home_rate, away_rate, self.rho)
                tau = tau[0] if tau else 1.0
                value = probability * tau
                grid[(home_goals, away_goals)] = value
                total += value
        return {key: value / total for key, value in grid.items()}

    def outcome_probabilities(self, home_team, away_team):
        home = draw = away = 0.0
        for (home_goals, away_goals), probability in self.grid(home_team, away_team).items():
            if home_goals > away_goals:
                home += probability
            elif home_goals < away_goals:
                away += probability
            else:
                draw += probability
        return {'home': home, 'draw': draw, 'away': away}

    def totals_probabilities(self, home_team, away_team, line):
        over = 0.0
        for (home_goals, away_goals), probability in self.grid(home_team, away_team).items():
            if home_goals + away_goals > line:
                over += probability
        return {'over': over, 'under': 1 - over}


def _ll_and_grad(matches, attack, defence, home, rho):
    """Log-likelihood and gradient for one parameter vector (log-scale parameters)."""
    gradient_attack = defaultdict(float)
    gradient_defence = defaultdict(float)
    gradient_home = 0.0
    gradient_rho = 0.0
    log_likelihood = 0.0
    for match in matches:
        home_team, away_team = match['home'], match['away']
        home_rate = exp(attack[home_team] + defence[away_team] + home)
        away_rate = exp(attack[away_team] + defence[home_team])
        if home_rate <= 0 or away_rate <= 0:
            return None
        x, y = match['home_goals'], match['away_goals']
        tau = _tau(x, y, home_rate, away_rate, rho)
        if tau is None:
            return None
        tau_value, d_home, d_away, d_rho = tau
        log_likelihood += (x * log(home_rate) - home_rate + y * log(away_rate) - away_rate
                           + log(tau_value))
        g_home = x - home_rate + d_home
        g_away = y - away_rate + d_away
        gradient_attack[home_team] += g_home
        gradient_defence[away_team] += g_home
        gradient_attack[away_team] += g_away
        gradient_defence[home_team] += g_away
        gradient_home += g_home
        gradient_rho += d_rho
    return log_likelihood, gradient_attack, gradient_defence, gradient_home, gradient_rho


def fit(matches, *, teams=None, max_iterations=400, tolerance=1e-7, ridge=1e-4):
    """Fit a Dixon-Coles model to chronological results.

    ``teams`` defaults to every club in ``matches``. The ridge term keeps a club with
    no goals from being driven to a boundary parameter and is negligible for someone
    with a season of results. Returns a :class:`Fit`; ``converged`` is explicit so a
    truncated run is visible instead of silently treated as a model.
    """
    if not matches:
        raise ValueError('cannot fit a model without completed matches')
    teams = sorted(teams or {match['home'] for match in matches} | {match['away'] for match in matches})
    attack = dict.fromkeys(teams, 0.0)
    defence = dict.fromkeys(teams, 0.0)
    home = 0.0
    rho = 0.0

    current = _ll_and_grad(matches, attack, defence, home, rho)
    if current is None:
        raise ValueError('non-positive initial scoring rate')
    current_ll = current[0]
    learning = 0.2
    converged = False
    iterations = 0
    gain = 0.0
    for iterations in range(1, max_iterations + 1):
        current = _ll_and_grad(matches, attack, defence, home, rho)
        if current is None:
            break
        current_ll, g_attack, g_defence, g_home, g_rho = current
        stepped = False
        for _ in range(12):
            candidate_attack = {team: attack[team] + learning * g_attack[team] - ridge * attack[team]
                                for team in teams}
            candidate_defence = {team: defence[team] + learning * g_defence[team] - ridge * defence[team]
                                 for team in teams}
            # Normalise the unidentifiable scale (attack * defence) without moving rates.
            shift = sum(candidate_attack.values()) / len(teams)
            candidate_attack = {team: value - shift for team, value in candidate_attack.items()}
            candidate_defence = {team: value + shift for team, value in candidate_defence.items()}
            candidate_home = home + learning * g_home
            candidate_rho = max(-RHO_BOUND, min(RHO_BOUND, rho + learning * g_rho))
            proposal = _ll_and_grad(matches, candidate_attack, candidate_defence, candidate_home, candidate_rho)
            if proposal is not None and proposal[0] >= current_ll:
                attack, defence, home, rho = candidate_attack, candidate_defence, candidate_home, candidate_rho
                gain = proposal[0] - current_ll
                current_ll = proposal[0]
                learning = min(learning * 1.1, 0.5)
                stepped = True
                break
            learning *= 0.5
        if not stepped:
            converged = True
            break
        if gain < tolerance * max(1.0, abs(current_ll)):
            converged = True
            break
    # Report the log-likelihood of the parameters actually returned, not the step before.
    final = _ll_and_grad(matches, attack, defence, home, rho)
    final_ll = final[0] if final else current_ll
    return Fit(attack, defence, home, rho, log_likelihood=final_ll,
               iterations=iterations, converged=converged, teams=teams)


def _closing_probabilities(match, book):
    prices = match.get('closing', {}).get(book)
    if not prices:
        return None
    try:
        inverse = {side: 1 / float(price) for side, price in prices.items()}
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    total = sum(inverse.values())
    if total <= 0:
        return None
    return {side: value / total for side, value in inverse.items()}


def evaluate(model, matches, *, book='AvgC', max_goals=MAX_GOALS):
    """Score 1X2 predictions against known results and public closing odds.

    Matches with an unseen club or without a complete closing triple are counted as
    coverage losses, never imputed. ``market_brier`` is only on the priced subset so a
    model and the market are always compared on identical fixtures.
    """
    missing = Counter()
    scored = []
    for match in matches:
        if match['home'] not in model.teams or match['away'] not in model.teams:
            missing['unseen_club'] += 1
            continue
        probabilities = model.outcome_probabilities(match['home'], match['away'])
        if abs(sum(probabilities.values()) - 1) > 1e-9:
            raise AssertionError('outcome probabilities are not normalised')
        market = _closing_probabilities(match, book)
        actual = match['result']
        row = {'probabilities': probabilities, 'actual': actual, 'market': market}
        scored.append(row)
        if market is None:
            missing[f'{book}_closing_absent'] += 1
    if not scored:
        return {'evidence_label': INSUFFICIENT, 'scored_matches': 0,
                'missing_reasons': dict(missing)}

    def brier(probabilities, outcome):
        return sum((probabilities[side] - (1.0 if side == outcome else 0.0)) ** 2
                   for side in ('home', 'draw', 'away'))

    model_brier = sum(brier(row['probabilities'], row['actual']) for row in scored) / len(scored)
    model_logloss = -sum(log(max(row['probabilities'][row['actual']], 1e-12)) for row in scored) / len(scored)
    priced = [row for row in scored if row['market'] is not None]
    market_brier = (sum(brier(row['market'], row['actual']) for row in priced) / len(priced)) if priced else None

    bin_counts = Counter()
    bin_totals = defaultdict(float)
    for row in scored:
        for side in ('home', 'draw', 'away'):
            probability = row['probabilities'][side]
            bucket = min(9, int(probability * 10))
            bin_counts[bucket] += 1
            bin_totals[bucket] += probability if side == row['actual'] else 0.0
    calibration = [
        {'bin': f'{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}', 'count': bin_counts[bucket],
         'mean_predicted': round(bin_totals[bucket] / bin_counts[bucket], 6)}
        for bucket in sorted(bin_counts)
    ]
    return {
        'evidence_label': MEASURED if len(scored) >= 50 else INSUFFICIENT,
        'scored_matches': len(scored),
        'priced_matches': len(priced),
        'model_brier': round(model_brier, 6),
        'model_log_loss': round(model_logloss, 6),
        'market_brier': None if market_brier is None else round(market_brier, 6),
        'market_book': book,
        'calibration': calibration,
        'missing_reasons': dict(missing),
    }


def split_by_season(matches, test_seasons):
    """Fixed chronological split: test seasons are held out entirely."""
    test = [match for match in matches if match['season'] in test_seasons]
    train = [match for match in matches if match['season'] not in test_seasons]
    return train, test


def run(public_dir, *, test_seasons=('2526',), book='AvgC', max_goals=MAX_GOALS):
    """Load the mounted public CSVs, fit on the past and evaluate the future."""
    document = load_directory(public_dir)
    matches = document['matches']
    train, test = split_by_season(matches, set(test_seasons))
    missing = Counter({f'csv_skipped_{key}': value for key, value in document['skipped'].items()})
    if not train or not test:
        return section(label=INSUFFICIENT, sources=document['provenance'], missing=missing,
                       limitations=['a fixed season split needs both a training and a test season'])
    model = fit(train)
    report = evaluate(model, test, book=book, max_goals=max_goals)
    results = {
        'training': {
            'matches': len(train), 'clubs': len(model.teams),
            'seasons': sorted({match['season'] for match in train}),
            'home_advantage': round(model.home, 6), 'rho': round(model.rho, 6),
            'log_likelihood': round(model.log_likelihood, 6),
            'iterations': model.iterations, 'converged': model.converged,
        },
        'evaluation': report,
        'test_seasons': sorted(test_seasons),
        'definition': '1X2 and totals summed from one normalised Dixon-Coles scoreline grid',
    }
    return section(
        label=report['evidence_label'],
        sources=document['provenance'],
        missing=missing,
        coverage={'training_matches': len(train), 'test_matches': len(test)},
        results=results,
        limitations=[
            'closing odds are a post-match reference and never enter the fit',
            'only 1X2 is scored because the public archive has no identical Asian line snapshot',
        ],
    )


if __name__ == '__main__':  # boundary + convergence self-check, no archives needed
    synthetic = [
        {'home': 'a', 'away': 'b', 'home_goals': 2, 'away_goals': 0},
        {'home': 'b', 'away': 'a', 'home_goals': 1, 'away_goals': 1},
    ] * 30
    fitted = fit(synthetic)
    assert fitted.converged, 'synthetic fit must converge'
    total = sum(fitted.grid('a', 'b').values())
    assert abs(total - 1) < 1e-9, total
    outcomes = fitted.outcome_probabilities('a', 'b')
    assert abs(sum(outcomes.values()) - 1) < 1e-9
    assert outcomes['home'] > outcomes['away'], outcomes
    print('dixon-coles self-check ok', round(fitted.log_likelihood, 3), fitted.iterations)
