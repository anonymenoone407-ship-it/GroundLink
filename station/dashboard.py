
import sqlite3
from contextlib import closing
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from anomaly_detector import detect_rf_anomaly


# ==========================================
# CONFIGURATION
# ==========================================

DB_PATH = (
    Path(__file__).resolve().parent
    / "state"
    / "monitoring.sqlite3"
)

st.set_page_config(
    page_title="GroundLink Guardian",
    page_icon="🛰️",
    layout="wide",
)


SCENARIO_LABELS = {
    "replay": "Replay", "spoofing": "Spoofing", "tampering": "Tampering",
    "ideal": "Ideal", "awgn_20db": "AWGN 20 dB", "awgn_0db": "AWGN 0 dB",
    "interference": "Jamming / Interference", "jamming": "Jamming / Interference",
    "unlabeled": "Non étiqueté (unlabeled)",
}
SCENARIO_ORDER = [
    "replay", "spoofing", "tampering", "ideal", "awgn_20db", "awgn_0db",
    "interference", "jamming",
]


# ==========================================
# LECTURE SQLITE (LECTURE SEULE)
# ==========================================

def load_events():
    if not DB_PATH.is_file():
        st.error(f"Base SQLite introuvable : {DB_PATH}")
        st.stop()

    query = """
        SELECT
            id,
            event_time,
            run_id,
            scenario,
            iq_bytes,
            power_db_rel,
            evm_percent,
            estimated_gain,
            residual_spectral_peak_db,
            status,
            reason,
            satellite_id,
            sequence_number,
            rf_level,
            rf_reasons,
            processing_ms
        FROM rx_events
        ORDER BY id
    """

    uri = f"{DB_PATH.as_uri()}?mode=ro"

    try:
        with closing(
            sqlite3.connect(uri, uri=True, timeout=5)
        ) as conn:
            return pd.read_sql_query(query, conn)

    except (sqlite3.Error, pd.errors.DatabaseError) as error:
        st.error(f"Erreur de lecture SQLite : {error}")
        st.stop()


# ==========================================
# RECALCUL HISTORIQUE DES DIAGNOSTICS RF
# ==========================================

def as_optional_float(value):
    if pd.isna(value):
        return None

    return float(value)


def add_historical_diagnostics(events):
    # Cette operation ne modifie jamais SQLite.
    events = events.copy()

    events["rf_origin"] = "ENREGISTRE"

    events["rf_level"] = (
        events["rf_level"].fillna("NON_CALCULE")
    )

    events["rf_reasons"] = (
        events["rf_reasons"].fillna("NON_CALCULE")
    )

    for idx in events.index[
        events["rf_level"] == "NON_CALCULE"
    ]:
        row = events.loc[idx]

        metrics = {
            "power_db_rel": as_optional_float(
                row["power_db_rel"]
            ),
            "evm_percent": as_optional_float(
                row["evm_percent"]
            ),
            "estimated_gain": as_optional_float(
                row["estimated_gain"]
            ),
            "residual_spectral_peak_db": as_optional_float(
                row["residual_spectral_peak_db"]
            ),
        }

        diagnosis = detect_rf_anomaly(metrics)

        events.at[idx, "rf_level"] = diagnosis["level"]

        events.at[idx, "rf_reasons"] = ", ".join(
            diagnosis["reasons"]
        )

        events.at[idx, "rf_origin"] = "RETRO_CALCULE"

    return events


# ==========================================
# RECEPTION SATELLITE EN DIRECT
# ==========================================

def latest_reception_events(events, limit=10):
    """Isole les derniers blocs recus, sans tenir compte des filtres UI."""
    return events.sort_values("id", ascending=False).head(limit).copy()


def display_value(value):
    if pd.isna(value) or value == "":
        return "—"
    return str(value)


def abbreviated_run_id(value, visible_chars=12):
    full_value = display_value(value)
    if full_value == "—" or len(full_value) <= visible_chars:
        return full_value
    return f"{full_value[:visible_chars]}…"


def render_live_reception(events):
    """Affiche au plus dix receptions reelles, independamment des filtres."""
    latest = latest_reception_events(events)
    received = len(latest)
    accepted = int((latest["status"] == "ACCEPT").sum())
    rejected = int((latest["status"] == "REJECT").sum())
    rows = []
    for _, event in latest.iterrows():
        status = display_value(event["status"])
        status_class = "live-accept" if status == "ACCEPT" else "live-reject" if status == "REJECT" else "live-neutral"
        rf_level = display_value(event["rf_level"])
        rf_label = "—" if rf_level == "—" else f"RF {rf_level}"
        rf_class = "live-rf-anomaly" if rf_level == "ANOMALY" else "live-rf-normal" if rf_level == "NORMAL" else "live-neutral"

        # Defense en profondeur pour d'eventuelles anciennes lignes incoherentes.
        trusted = status == "ACCEPT"
        satellite_id = display_value(event["satellite_id"]) if trusted else "—"
        sequence_number = display_value(event["sequence_number"]) if trusted else "—"
        if sequence_number != "—":
            try:
                sequence_number = str(int(float(sequence_number)))
            except (TypeError, ValueError, OverflowError):
                pass

        evm = event["evm_percent"]
        evm_display = "—" if pd.isna(evm) else f"{float(evm):.2f}"
        run_id = display_value(event["run_id"])
        short_run_id = abbreviated_run_id(event["run_id"])
        reason = display_value(event["reason"])
        rows.append(
            "<tr>"
            f"<td class='live-mono'>{escape(display_value(event['id']))}</td>"
            f"<td class='live-mono'>{escape(display_value(event['event_time']))}</td>"
            f"<td>{escape(display_value(event['scenario']))}</td>"
            f"<td class='live-mono' title='{escape(run_id)}'>{escape(short_run_id)}</td>"
            f"<td class='live-mono'>{escape(satellite_id)}</td>"
            f"<td class='live-mono'>{escape(sequence_number)}</td>"
            f"<td class='live-mono'>{escape(evm_display)}</td>"
            f"<td><span class='{rf_class}'>{escape(rf_label)}</span></td>"
            f"<td><span class='{status_class}'>{escape(status)}</span></td>"
            f"<td><span class='live-reason'>{escape(reason)}</span></td></tr>"
        )

    table_rows = "".join(rows) if rows else "<tr><td colspan='10' class='live-empty'>Aucun evenement recu dans SQLite.</td></tr>"
    card = f"""
    <style>
      .live-card {{ background:var(--secondary-background-color,#fff); border:1px solid rgba(128,128,128,.3); border-radius:14px; padding:1.25rem; margin:.35rem 0 1rem; box-shadow:0 3px 14px rgba(0,0,0,.06); }}
      .live-title {{ font-size:1.4rem; font-weight:750; line-height:1.25; }} .live-subtitle {{ opacity:.7; font-size:.86rem; margin-top:.2rem; }}
      .live-metrics {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.7rem; margin:1rem 0; }}
      .live-metric {{ border:1px solid rgba(128,128,128,.25); border-radius:10px; padding:.7rem .8rem; }} .live-metric-label {{ opacity:.72; font-size:.78rem; }}
      .live-metric-value {{ font:700 1.55rem ui-monospace,SFMono-Regular,Menlo,monospace; }} .live-green,.live-accept {{ color:#15803d; }} .live-red,.live-reject {{ color:#dc2626; }}
      .live-table-wrap {{ overflow-x:auto; }} .live-table {{ width:100%; min-width:1050px; border-collapse:collapse; font-size:.83rem; }}
      .live-table th,.live-table td {{ text-align:left; padding:.62rem .5rem; border-top:1px solid rgba(128,128,128,.22); vertical-align:middle; white-space:nowrap; }}
      .live-table th {{ opacity:.7; font-size:.74rem; }} .live-mono,.live-reason {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
      .live-accept,.live-reject,.live-neutral,.live-rf-anomaly,.live-rf-normal {{ display:inline-block; border-radius:999px; padding:.14rem .5rem; font-weight:750; }}
      .live-accept {{ background:rgba(34,197,94,.14); }} .live-reject {{ background:rgba(239,68,68,.14); }}
      .live-rf-anomaly {{ color:#c2410c; background:rgba(249,115,22,.15); }} .live-rf-normal {{ color:#15803d; background:rgba(34,197,94,.12); }}
      .live-neutral {{ background:rgba(128,128,128,.14); }} .live-reason {{ font-weight:650; }} .live-empty {{ text-align:center!important; opacity:.7; padding:1.2rem!important; }}
      @media(max-width:700px) {{ .live-card{{padding:.85rem}} .live-metrics{{gap:.35rem}} .live-metric{{padding:.55rem}} .live-metric-value{{font-size:1.25rem}} }}
    </style>
    <section class="live-card" aria-label="Live Satellite Reception">
      <div class="live-title">Live Satellite Reception — Last 10 Events</div>
      <div class="live-subtitle">Latest received IQ blocks · SQLite monitoring</div>
      <div class="live-metrics">
        <div class="live-metric"><div class="live-metric-label">Received</div><div class="live-metric-value">{received}</div></div>
        <div class="live-metric"><div class="live-metric-label">ACCEPT</div><div class="live-metric-value live-green">{accepted}</div></div>
        <div class="live-metric"><div class="live-metric-label">REJECT</div><div class="live-metric-value live-red">{rejected}</div></div>
      </div>
      <div class="live-table-wrap"><table class="live-table"><thead><tr>
        <th>Event ID</th><th>Reception Time</th><th>Scenario</th><th>Run ID</th><th>Satellite ID</th>
        <th>Sequence Number</th><th>EVM (%)</th><th>RF Status</th><th>Security Decision</th><th>Decision Reason</th>
      </tr></thead><tbody>{table_rows}</tbody></table></div>
    </section>
    """
    st.markdown(card, unsafe_allow_html=True)
    export_columns = [
        "id", "event_time", "scenario", "run_id", "satellite_id",
        "sequence_number", "evm_percent", "rf_level", "status", "reason",
    ]
    export_data = latest[export_columns].copy()
    rejected_mask = export_data["status"] != "ACCEPT"
    export_data.loc[rejected_mask, ["satellite_id", "sequence_number"]] = None
    st.download_button(
        "⬇️ Exporter les 10 derniers evenements en CSV",
        data=export_data.to_csv(index=False).encode("utf-8"),
        file_name="groundlink_latest_reception.csv", mime="text/csv",
        key="live_reception_export",
    )


# ==========================================
# RESULTATS DES SCENARIOS D'ATTAQUE
# ==========================================

def available_scenarios(events):
    """Retourne uniquement les scenarios reellement persistes."""
    persisted = {str(value) for value in events["scenario"].dropna().unique()}
    ordered = [value for value in SCENARIO_ORDER if value in persisted]
    ordered.extend(sorted(persisted.difference(ordered)))
    return ordered


def scenario_label(value):
    return SCENARIO_LABELS.get(value, value)


def runs_for_scenario(events, scenario):
    """Classe les runs du plus recent au plus ancien via l'id persiste."""
    matching = events[events["scenario"] == scenario].dropna(subset=["run_id"])
    if matching.empty:
        return []
    return (
        matching.groupby("run_id", sort=False)["id"].max()
        .sort_values(ascending=False).index.astype(str).tolist()
    )


def rf_security_interpretation(rf_level, status):
    if rf_level == "NORMAL" and status == "REJECT":
        return "RF NORMAL + REJECT — rejet cryptographique / integrite"
    if rf_level == "ANOMALY" and status == "ACCEPT":
        return "RF ANOMALY + ACCEPT — interference, trame authentifiee"
    if rf_level == "ANOMALY" and status == "REJECT":
        return "RF ANOMALY + REJECT — perturbation severe, trame corrompue"
    return f"RF {rf_level} + {status}"


def render_attack_results(events):
    st.header("Attack Scenario Results — Résultats des attaques")
    st.caption(
        "Resultats historiques authentiques lus depuis la base SQLite "
        "de monitoring en lecture seule."
    )
    scenarios = available_scenarios(events)
    if not scenarios:
        st.info("Aucun evenement de scenario n'est disponible dans SQLite.")
        return

    selector_left, selector_right = st.columns(2)
    with selector_left:
        selected_scenario = st.selectbox(
            "Scenario", scenarios, format_func=scenario_label,
            key="attack_results_scenario",
        )
    run_ids = runs_for_scenario(events, selected_scenario)
    if not run_ids:
        st.info("Aucune execution disponible pour ce scenario.")
        return
    with selector_right:
        selected_run = st.selectbox(
            "Run ID (execution la plus recente par defaut)", run_ids,
            key=f"attack_results_run_{selected_scenario}",
        )

    run = events[
        (events["scenario"] == selected_scenario)
        & (events["run_id"].astype(str) == selected_run)
    ].sort_values("id").copy()
    if run.empty:
        st.info("Aucun evenement disponible pour cette execution.")
        return

    total = len(run)
    accept_count = int((run["status"] == "ACCEPT").sum())
    reject_count = int((run["status"] == "REJECT").sum())
    first_time, last_time = str(run.iloc[0]["event_time"]), str(run.iloc[-1]["event_time"])
    rf_diagnostics = ", ".join(sorted(run["rf_level"].fillna("UNKNOWN").astype(str).unique()))
    rejection_reasons = ", ".join(sorted(
        run.loc[run["status"] == "REJECT", "reason"]
        .fillna("UNKNOWN_REASON").astype(str).unique()
    )) or "Aucun rejet"

    rows = []
    for block_number, (_, event) in enumerate(run.iterrows(), start=1):
        status = str(event["status"])
        reason = "UNKNOWN_REASON" if pd.isna(event["reason"]) else str(event["reason"])
        rf_level = "UNKNOWN" if pd.isna(event["rf_level"]) else str(event["rf_level"])
        rf_reasons = "NON_CALCULE" if pd.isna(event["rf_reasons"]) else str(event["rf_reasons"])
        status_class = "gl-accept" if status == "ACCEPT" else "gl-reject"
        interpretation = rf_security_interpretation(rf_level, status)
        rows.append(
            "<tr>"
            f"<td class='gl-mono'>Bloc {block_number}</td>"
            f"<td class='gl-mono'>{escape(str(event['event_time']))}</td>"
            f"<td><span class='{status_class}'>{escape(status)}</span></td>"
            f"<td class='gl-mono'>{escape(reason)}</td>"
            f"<td><span class='gl-rf'>{escape(rf_level)}</span> "
            f"<span class='gl-mono'>{escape(rf_reasons)}</span>"
            f"<div class='gl-detail'>{escape(interpretation)}</div></td></tr>"
        )

    title = f"{scenario_label(selected_scenario)} Attack — Résultats"
    card = f"""
    <style>
      .gl-card {{ background: var(--secondary-background-color, #fff); border: 1px solid rgba(128,128,128,.28); border-radius: 14px; padding: 1.35rem; margin: .6rem 0 1rem; box-shadow: 0 2px 12px rgba(0,0,0,.04); }}
      .gl-title {{ font-size: 1.45rem; font-weight: 700; margin-bottom: .2rem; }}
      .gl-subtitle,.gl-detail {{ color: var(--text-color); opacity: .72; font-size: .84rem; }}
      .gl-metrics {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.75rem; margin:1.1rem 0; }}
      .gl-metric {{ border:1px solid rgba(128,128,128,.24); border-radius:10px; padding:.8rem; }}
      .gl-metric-label {{ font-size:.8rem; opacity:.72; }}
      .gl-metric-value {{ font:700 1.65rem ui-monospace,SFMono-Regular,Menlo,monospace; }}
      .gl-green {{ color:#16a34a; }} .gl-red {{ color:#dc2626; }}
      .gl-meta {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.35rem 1rem; font-size:.88rem; margin-bottom:1rem; overflow-wrap:anywhere; }}
      .gl-table-wrap {{ overflow-x:auto; }} .gl-table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
      .gl-table th,.gl-table td {{ text-align:left; padding:.7rem .55rem; border-top:1px solid rgba(128,128,128,.22); vertical-align:top; }}
      .gl-table th {{ font-size:.78rem; opacity:.72; }} .gl-mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
      .gl-accept,.gl-reject,.gl-rf {{ display:inline-block; border-radius:999px; padding:.12rem .5rem; font:700 .76rem ui-monospace,SFMono-Regular,Menlo,monospace; }}
      .gl-accept {{ color:#15803d; background:rgba(34,197,94,.13); }} .gl-reject {{ color:#dc2626; background:rgba(239,68,68,.13); }} .gl-rf {{ background:rgba(128,128,128,.15); }}
      @media(max-width:700px) {{ .gl-card{{padding:.9rem}} .gl-metrics{{grid-template-columns:1fr}} .gl-meta{{grid-template-columns:1fr}} .gl-table{{min-width:760px}} }}
    </style>
    <section class="gl-card">
      <div class="gl-title">{escape(title)}</div>
      <div class="gl-subtitle">Donnees persistantes SQLite — une seule execution selectionnee</div>
      <div class="gl-metrics">
        <div class="gl-metric"><div class="gl-metric-label">Nombre de blocs recus</div><div class="gl-metric-value">{total}</div></div>
        <div class="gl-metric"><div class="gl-metric-label">ACCEPT</div><div class="gl-metric-value gl-green">{accept_count}</div></div>
        <div class="gl-metric"><div class="gl-metric-label">REJECT</div><div class="gl-metric-value gl-red">{reject_count}</div></div>
      </div>
      <div class="gl-meta">
        <div><strong>Run ID</strong> <span class="gl-mono">{escape(selected_run)}</span></div>
        <div><strong>Scenario SQLite</strong> <span class="gl-mono">{escape(selected_scenario)}</span></div>
        <div><strong>Reception</strong> <span class="gl-mono">{escape(first_time)} — {escape(last_time)}</span></div>
        <div><strong>Diagnostic RF</strong> <span class="gl-mono">{escape(rf_diagnostics)}</span></div>
        <div><strong>Raison(s) de rejet</strong> <span class="gl-mono">{escape(rejection_reasons)}</span></div>
      </div>
      <div class="gl-table-wrap"><table class="gl-table"><thead><tr><th>Bloc</th><th>Date / heure de reception</th><th>Statut</th><th>Raison</th><th>Diagnostic RF</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    </section>
    """
    st.markdown(card, unsafe_allow_html=True)

    export_columns = [
        "id", "event_time", "run_id", "scenario", "status", "reason",
        "rf_level", "rf_reasons", "evm_percent", "power_db_rel",
        "estimated_gain", "residual_spectral_peak_db", "processing_ms",
    ]
    st.download_button(
        "⬇️ Exporter ce run en CSV",
        data=run[export_columns].to_csv(index=False).encode("utf-8"),
        file_name=f"groundlink_{selected_scenario}_{selected_run}.csv",
        mime="text/csv", key=f"attack_results_export_{selected_scenario}_{selected_run}",
    )


# ==========================================
# EN-TETE
# ==========================================

st.title("🛰️ GroundLink Guardian")

# Charge avant tout filtre : la carte live couvre tous les scenarios et runs.
df = load_events()
render_live_reception(df)
df = add_historical_diagnostics(df)

st.subheader("Secure Satellite Ocean-Wind Downlink")

st.caption(
    "RF Monitoring | Anomaly Detection | "
    "Ed25519 Authentication | Anti-Replay"
)

if st.button("🔄 Actualiser les donnees"):
    st.rerun()


# ==========================================
# VALIDATION DES DONNEES POUR LES VUES HISTORIQUES
# ==========================================

if df.empty:
    st.warning("Aucune observation enregistree.")
    st.stop()

render_attack_results(df)


# ==========================================
# FILTRES
# ==========================================

st.sidebar.header("Filtres")

scenarios = (
    ["Tous"]
    + sorted(df["scenario"].dropna().unique().tolist())
)

selected_scenario = st.sidebar.selectbox(
    "Scenario RF",
    scenarios,
)

selected_status = st.sidebar.selectbox(
    "Decision de securite",
    ["Toutes", "ACCEPT", "REJECT"],
)

filtered = df.copy()

if selected_scenario != "Tous":
    filtered = filtered[
        filtered["scenario"] == selected_scenario
    ]

if selected_status != "Toutes":
    filtered = filtered[
        filtered["status"] == selected_status
    ]


# ==========================================
# VUE GENERALE
# ==========================================

total = len(filtered)

accepted = int(
    (filtered["status"] == "ACCEPT").sum()
)

rejected = int(
    (filtered["status"] == "REJECT").sum()
)

anomalies = int(
    (filtered["rf_level"] == "ANOMALY").sum()
)

st.header("Vue generale")

c1, c2, c3, c4 = st.columns(4)

c1.metric("IQ recus", total)
c2.metric("ACCEPT", accepted)
c3.metric("REJECT", rejected)
c4.metric("RF ANOMALY", anomalies)


# ==========================================
# CLASSIFICATION RF
# ==========================================

st.header("Diagnostic RF")

levels = [
    "NORMAL",
    "DEGRADED",
    "ANOMALY",
    "UNKNOWN",
    "NON_CALCULE",
]

level_counts = (
    filtered["rf_level"]
    .value_counts()
    .reindex(levels, fill_value=0)
)

st.bar_chart(level_counts)

st.caption(
    "ENREGISTRE : diagnostic produit pendant la reception. "
    "RETRO_CALCULE : diagnostic reconstruit pour l'affichage "
    "a partir des metriques historiques. "
    "UNKNOWN : metriques insuffisantes."
)


# ==========================================
# SECURITE PAR SCENARIO
# ==========================================

st.header("Securite par scenario")

if filtered.empty:
    st.info("Aucune donnee pour les filtres choisis.")

else:
    security_counts = (
        filtered.groupby(["scenario", "status"])
        .size()
        .unstack(fill_value=0)
        .reindex(
            columns=["ACCEPT", "REJECT"],
            fill_value=0,
        )
    )

    st.bar_chart(security_counts)


# ==========================================
# CENTRE D'ALERTES
# ==========================================

st.header("Centre d'alertes")

# Anomalies RF
rf_alerts = filtered[
    filtered["rf_level"] == "ANOMALY"
]

# Toutes les trames rejetees
rejected_events = filtered[
    filtered["status"] == "REJECT"
]

# Alertes liees a la signature et au rejeu
security_reasons = [
    "SIGNATURE_INVALID",
    "REPLAY_DETECTED",
]

security_alerts = rejected_events[
    rejected_events["reason"].isin(security_reasons)
]


# ------------------------------------------
# COMPTEURS DES ALERTES
# ------------------------------------------

a1, a2, a3 = st.columns(3)

a1.metric(
    "Anomalies RF",
    len(rf_alerts)
)

a2.metric(
    "Trames rejetees",
    len(rejected_events)
)

a3.metric(
    "Alertes authentification / rejeu",
    len(security_alerts)
)


# ------------------------------------------
# MOTIFS DES REJETS
# ------------------------------------------

st.subheader("Motifs des rejets")

if rejected_events.empty:

    st.success(
        "Aucune trame rejetee dans la selection."
    )

else:

    reason_counts = (
        rejected_events["reason"]
        .fillna("UNKNOWN_REASON")
        .value_counts()
        .rename_axis("Motif")
        .reset_index(name="Occurrences")
    )

    st.dataframe(
        reason_counts,
        use_container_width=True,
        hide_index=True,
    )


# ------------------------------------------
# HISTORIQUE DES ALERTES
# ------------------------------------------

st.subheader("Historique des alertes")

alerts = filtered[
    (filtered["rf_level"] == "ANOMALY")
    | (filtered["status"] == "REJECT")
]

alerts = alerts.sort_values(
    "id",
    ascending=False
)

alert_columns = [
    "id",
    "event_time",
    "scenario",
    "rf_level",
    "rf_reasons",
    "status",
    "reason",
    "rf_origin",
]

if alerts.empty:

    st.info("Aucune alerte dans la selection.")

else:

    st.dataframe(
        alerts[alert_columns],
        use_container_width=True,
        hide_index=True,
    )

st.caption(
    "RF ANOMALY indique une deviation radio, "
    "pas une preuve d'attaque. "
    "SIGNATURE_INVALID et REPLAY_DETECTED "
    "sont distingues des perturbations RF "
    "et des erreurs de decodage."
)


# ==========================================
# GRAPHIQUES RF
# ==========================================

st.header("Evolution du signal RF")

left, right = st.columns(2)

with left:

    st.subheader("EVM (%)")

    evm_data = (
        filtered[["id", "evm_percent"]]
        .dropna()
        .set_index("id")
    )

    if not evm_data.empty:
        st.line_chart(evm_data)

    else:
        st.info("Aucune mesure EVM.")


with right:

    st.subheader("Puissance (dB relatif)")

    power_data = (
        filtered[["id", "power_db_rel"]]
        .dropna()
        .set_index("id")
    )

    if not power_data.empty:
        st.line_chart(power_data)

    else:
        st.info("Aucune mesure de puissance.")


# ==========================================
# HISTORIQUE COMPLET
# ==========================================

st.header("Historique des observations")

columns = [
    "id",
    "event_time",
    "scenario",
    "rf_level",
    "rf_reasons",
    "rf_origin",
    "status",
    "reason",
    "evm_percent",
    "power_db_rel",
    "estimated_gain",
    "residual_spectral_peak_db",
    "processing_ms",
]

display_df = (
    filtered
    .sort_values("id", ascending=False)
    [columns]
    .copy()
)

numeric_columns = [
    "evm_percent",
    "power_db_rel",
    "estimated_gain",
    "residual_spectral_peak_db",
    "processing_ms",
]

display_df[numeric_columns] = (
    display_df[numeric_columns].round(2)
)

st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "Le diagnostic RF et la decision cryptographique "
    "sont independants. "
    "Les seuils RF sont provisoires et doivent etre "
    "evalues sur des tests independants."
)




