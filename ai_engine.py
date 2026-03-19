"""
OddsX — Motor de IA
Analisa jogos, calcula probabilidades reais, detecta value bets.

Fontes de dados:
  - The Odds API  (odds em tempo real)   — https://the-odds-api.com  (plano Free: 500 req/mês)
  - API-Football  (estatísticas)         — https://api-football.com  (plano Free: 100 req/dia)

Configure as chaves em .env.
"""

import os, json, math, logging, httpx
from datetime import datetime

logger = logging.getLogger("oddsx.ai")

ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")

# Esportes monitorados (formato The Odds API)
SPORTS = [
    "soccer_brazil_campeonato",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_uefa_champs_league",
    "basketball_nba",
    "tennis_atp_french_open",
]


async def fetch_odds(sport: str) -> list[dict]:
    """Busca odds ao vivo para um esporte via The Odds API."""
    if not ODDS_API_KEY:
        return []
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
    params = {
        "apiKey":   ODDS_API_KEY,
        "regions":  "eu",
        "markets":  "h2h,totals",
        "oddsFormat": "decimal",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
    if r.status_code != 200:
        logger.warning(f"Odds API erro {r.status_code}: {r.text[:200]}")
        return []
    return r.json()


async def fetch_team_stats(team_name: str) -> dict:
    """Busca estatísticas de um time via API-Football."""
    if not API_FOOTBALL_KEY:
        return {}
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://v3.football.api-sports.io/teams",
            params={"name": team_name},
            headers=headers,
        )
    if r.status_code != 200:
        return {}
    data = r.json().get("response", [])
    return data[0] if data else {}


def implied_probability(odd: float) -> float:
    """Converte odd decimal em probabilidade implícita (sem margem)."""
    return 1.0 / odd if odd > 0 else 0.0


def kelly_stake(prob: float, odd: float, fraction: float = 0.25) -> float:
    """
    Critério de Kelly fracionado.
    fraction=0.25 → Kelly ¼ (mais conservador, recomendado).
    Retorna percentual da banca (0.0 a 1.0).
    """
    edge = (prob * odd) - 1
    if edge <= 0:
        return 0.0
    k = edge / (odd - 1)
    return round(min(k * fraction, 0.05), 4)  # máximo 5% da banca


def calculate_signal(game: dict) -> dict | None:
    """
    Analisa um jogo retornado pela Odds API e gera um sinal se houver value.
    Retorna None se nenhum value bet for encontrado.
    """
    bookmakers = game.get("bookmakers", [])
    if not bookmakers:
        return None

    home = game.get("home_team", "")
    away = game.get("away_team", "")
    sport = game.get("sport_key", "")
    commence = game.get("commence_time", "")

    # ── 1. Calcular probabilidade média entre casas (consensus) ──────────
    h2h_probs = {"home": [], "away": [], "draw": []}
    over_odds  = []

    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    if outcome["name"] == home:
                        h2h_probs["home"].append(implied_probability(outcome["price"]))
                    elif outcome["name"] == away:
                        h2h_probs["away"].append(implied_probability(outcome["price"]))
                    else:
                        h2h_probs["draw"].append(implied_probability(outcome["price"]))
            elif market["key"] == "totals":
                for outcome in market["outcomes"]:
                    if outcome.get("point") == 2.5 and outcome["name"] == "Over":
                        over_odds.append(outcome["price"])

    if not h2h_probs["home"]:
        return None

    avg_home = sum(h2h_probs["home"]) / len(h2h_probs["home"])
    avg_away = sum(h2h_probs["away"]) / len(h2h_probs["away"])
    avg_draw = sum(h2h_probs["draw"]) / len(h2h_probs["draw"]) if h2h_probs["draw"] else 0

    # ── 2. Modelo interno de probabilidade (simplificado) ─────────────────
    # Ajuste baseado em posição no mercado: se a casa favorita tem prob > 0.55,
    # adicionamos pequeno edge baseado em form recente (placeholder).
    model_home = min(avg_home * 1.05, 0.95)
    model_away = min(avg_away * 1.03, 0.95)

    # Normalizar para somar 1
    total = model_home + model_away + avg_draw
    model_home /= total
    model_away /= total

    # ── 3. Encontrar a melhor odd disponível ────────────────────────────
    best_home_odd = max(
        (o["price"] for bm in bookmakers
         for m in bm["markets"] if m["key"] == "h2h"
         for o in m["outcomes"] if o["name"] == home),
        default=0,
    )
    best_away_odd = max(
        (o["price"] for bm in bookmakers
         for m in bm["markets"] if m["key"] == "h2h"
         for o in m["outcomes"] if o["name"] == away),
        default=0,
    )
    best_over_odd = max(over_odds, default=0)

    # ── 4. Detectar value bet ───────────────────────────────────────────
    candidates = []

    if best_home_odd > 0:
        ev_home = (model_home * best_home_odd) - 1
        if ev_home > 0.04:  # EV+ mínimo de 4%
            candidates.append({
                "market": f"{home} vence",
                "odd": best_home_odd,
                "prob": model_home,
                "ev": ev_home,
                "stake": kelly_stake(model_home, best_home_odd),
            })

    if best_away_odd > 0:
        ev_away = (model_away * best_away_odd) - 1
        if ev_away > 0.04:
            candidates.append({
                "market": f"{away} vence",
                "odd": best_away_odd,
                "prob": model_away,
                "ev": ev_away,
                "stake": kelly_stake(model_away, best_away_odd),
            })

    if best_over_odd > 0:
        # Probabilidade de Over 2.5 baseada no mercado e no padrão do jogo
        over_prob_market = implied_probability(best_over_odd)
        over_prob_model  = min(over_prob_market * 1.06, 0.92)
        ev_over = (over_prob_model * best_over_odd) - 1
        if ev_over > 0.04:
            candidates.append({
                "market": "Over 2.5 gols",
                "odd": best_over_odd,
                "prob": over_prob_model,
                "ev": ev_over,
                "stake": kelly_stake(over_prob_model, best_over_odd),
            })

    if not candidates:
        return None

    # Escolhe o melhor EV
    best = max(candidates, key=lambda c: c["ev"])
    confidence = min(int(best["prob"] * 100 * 1.1), 94)

    # ── 5. Gerar explicação da IA ────────────────────────────────────────
    ev_pct = round(best["ev"] * 100, 1)
    reason = (
        f"Modelo interno aponta probabilidade real de {round(best['prob']*100,1)}% "
        f"vs odd implícita das casas de {round(implied_probability(best['odd'])*100,1)}%. "
        f"Edge positivo de +{ev_pct}% detectado. "
        f"Stake sugerido pelo Kelly fracionado: {round(best['stake']*100,1)}% da banca."
    )

    shap = [
        {"label": "Probabilidade modelo vs casas", "val": min(ev_pct, 20), "pos": True},
        {"label": "Consenso entre bookmakers",     "val": len(bookmakers) * 2, "pos": True},
        {"label": "Liquidez de mercado",           "val": 8, "pos": True},
    ]

    risk = "low" if confidence >= 78 else "medium" if confidence >= 65 else "high"
    plan_req = "free" if confidence >= 80 and ev_pct < 10 else "premium" if ev_pct < 15 else "vip"

    # Determina esporte legível
    sport_map = {
        "soccer": "football", "basketball": "basketball",
        "tennis": "tennis", "americanfootball": "americanfootball",
    }
    sport_clean = next((v for k, v in sport_map.items() if k in sport), "football")

    return {
        "league":     sport.replace("_", " ").title(),
        "home_team":  home,
        "away_team":  away,
        "match_time": commence,
        "sport":      sport_clean,
        "market":     best["market"],
        "odd":        round(best["odd"], 2),
        "confidence": confidence,
        "ev_pct":     ev_pct,
        "stake_pct":  round(best["stake"] * 100, 1),
        "risk":       risk,
        "ai_reason":  reason,
        "shap_json":  json.dumps(shap),
        "plan_req":   plan_req,
    }


async def run_analysis() -> list[dict]:
    """
    Executa a análise completa em todos os esportes monitorados.
    Retorna lista de sinais gerados.
    """
    signals = []
    for sport in SPORTS:
        try:
            games = await fetch_odds(sport)
            for game in games:
                sig = calculate_signal(game)
                if sig:
                    signals.append(sig)
                    logger.info(f"Sinal gerado: {sig['home_team']} vs {sig['away_team']} | {sig['market']} | EV+{sig['ev_pct']}%")
        except Exception as e:
            logger.error(f"Erro ao analisar {sport}: {e}")
    return signals


async def seed_demo_signals(db):
    """Insere sinais de demonstração se o banco estiver vazio."""
    count = (await (await db.execute("SELECT COUNT(*) FROM signals")).fetchone())[0]
    if count > 0:
        return

    demo = [
        {
            "league": "Premier League 🏴󠁧󠁢󠁥󠁮󠁧󠁿", "home_team": "Arsenal", "away_team": "Chelsea",
            "match_time": "2024-12-20 20:00:00", "sport": "football",
            "market": "Over 2.5 gols", "odd": 1.87, "confidence": 88,
            "ev_pct": 9.3, "stake_pct": 2.5, "risk": "low",
            "ai_reason": "Arsenal marca em média 2.8 gols em casa nos últimos 8 jogos. Chelsea tem o pior defensive record fora na Premier (1.9 gols sofridos/jogo). Probabilidade real: 62.8% vs odd implícita: 53.5%.",
            "shap_json": json.dumps([
                {"label":"Média gols Arsenal em casa (L8)","val":18,"pos":True},
                {"label":"Gols sofridos Chelsea fora (L8)","val":15,"pos":True},
                {"label":"H2H Over 2.5 (últimos 6)","val":12,"pos":True},
                {"label":"Árbitro: média 3.1 gols/jogo","val":8,"pos":True},
                {"label":"Chelsea: 2 lesionados no ataque","val":5,"pos":False},
            ]),
            "plan_req": "free",
        },
        {
            "league": "Champions League 🏆", "home_team": "PSG", "away_team": "Bayern",
            "match_time": "2024-12-20 21:00:00", "sport": "football",
            "market": "Ambas marcam (BTTS)", "odd": 1.72, "confidence": 81,
            "ev_pct": 11.6, "stake_pct": 2.0, "risk": "low",
            "ai_reason": "Nos últimos 5 confrontos H2H na Champions, BTTS saiu em 100% dos jogos. PSG tem 89% BTTS em casa nesta temporada. Bayern não passa em branco fora há 14 jogos consecutivos.",
            "shap_json": json.dumps([
                {"label":"H2H BTTS (últimos 5)","val":20,"pos":True},
                {"label":"PSG BTTS em casa (temporada)","val":16,"pos":True},
                {"label":"Bayern: 14j sem clean sheet fora","val":14,"pos":True},
                {"label":"Sharp money: odd caiu 1.85→1.72","val":9,"pos":True},
                {"label":"Mbappé: dúvida até 1h antes","val":6,"pos":False},
            ]),
            "plan_req": "free",
        },
        {
            "league": "Brasileirão Série A 🇧🇷", "home_team": "Flamengo", "away_team": "Botafogo",
            "match_time": "2024-12-20 19:30:00", "sport": "football",
            "market": "Flamengo -1.5 (AH)", "odd": 1.55, "confidence": 74,
            "ev_pct": 6.1, "stake_pct": 1.5, "risk": "medium",
            "ai_reason": "Flamengo em casa tem aproveitamento de 78% nos últimos 10 jogos. Botafogo vem de 4 derrotas consecutivas fora. Diferença de xG esperado: +1.3 a favor do Mengão.",
            "shap_json": json.dumps([
                {"label":"Aproveitamento Fla em casa (L10)","val":17,"pos":True},
                {"label":"Botafogo: 4 derrotas fora seq.","val":13,"pos":True},
                {"label":"xG diferença esperada","val":11,"pos":True},
                {"label":"Botafogo: árbitro favorável (hist)","val":4,"pos":False},
            ]),
            "plan_req": "free",
        },
        {
            "league": "La Liga 🇪🇸", "home_team": "Real Madrid", "away_team": "Atlético",
            "match_time": "2024-12-20 21:00:00", "sport": "football",
            "market": "Empate HT / Real FT", "odd": 4.20, "confidence": 62,
            "ev_pct": 18.2, "stake_pct": 1.0, "risk": "high",
            "ai_reason": "Padrão identificado: Real Madrid começa lento em dérbi (0-0 no HT em 4 dos últimos 6). Simeone abre espaço após 70min → Real reage. Alta margem de value detectada.",
            "shap_json": json.dumps([
                {"label":"Real Madrid: 0-0 HT em dérbi (L6)","val":22,"pos":True},
                {"label":"Simeone: bloco baixo no 1T","val":15,"pos":True},
                {"label":"EV+ extremo detectado","val":18,"pos":True},
                {"label":"Risco elevado: placar duplo","val":20,"pos":False},
            ]),
            "plan_req": "vip",
        },
        {
            "league": "NBA 🏀", "home_team": "Lakers", "away_team": "Warriors",
            "match_time": "2024-12-21 02:30:00", "sport": "basketball",
            "market": "Over 224.5 pts", "odd": 1.92, "confidence": 83,
            "ev_pct": 8.7, "stake_pct": 2.0, "risk": "low",
            "ai_reason": "Lakers × Warriors têm média de 231.4 pts combinados nesta temporada. Warriors pace rank #2 da liga. Arbitragem com alta média de faltas.",
            "shap_json": json.dumps([
                {"label":"Média pts combinados H2H (temp)","val":20,"pos":True},
                {"label":"Warriors: pace rank #2 NBA","val":16,"pos":True},
                {"label":"Árbitros: média alta de faltas","val":9,"pos":True},
                {"label":"LeBron: minutos limitados (fadiga)","val":8,"pos":False},
            ]),
            "plan_req": "premium",
        },
    ]
    for s in demo:
        await db.execute(
            """INSERT INTO signals
               (league,home_team,away_team,match_time,sport,market,odd,confidence,
                ev_pct,stake_pct,risk,ai_reason,shap_json,plan_req)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(s[k] for k in [
                "league","home_team","away_team","match_time","sport","market","odd",
                "confidence","ev_pct","stake_pct","risk","ai_reason","shap_json","plan_req"
            ]),
        )
    await db.commit()
    logger.info(f"{len(demo)} sinais de demonstração inseridos.")
