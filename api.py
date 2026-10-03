"""BLACK VEIL v2 — synthetic intelligence correlation prototype API."""
import copy, csv, io, hashlib, json, random, sqlite3, uuid
from datetime import datetime, timedelta
from dataclasses import asdict
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from core.synthetic_data import generate_dataset, Identity, _make_identity
from core.convergence_engine import compute_actor_linkage, DEFAULT_WEIGHTS, CONTRADICTION_MULTIPLIER
from core.graph_builder import build_case_graph
from core.integrity import build_integrity_chain, hash_identity_evidence
from core.report_generator import generate_report, generate_pdf_report
from core.evaluate import evaluate_on_test_set, load_tuned_weights
from core import similarity as sim

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "black_veil_cases.sqlite3"
app = FastAPI(title="BLACK VEIL — Dark Web Intelligence Correlation Platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 100-record intelligence library. The underlying records remain rich; the UI exposes only lookup fields.
_BASE = generate_dataset(9, 42)
_IDENTITIES = dict(_BASE["identities"])
rng = random.Random(4242)
_extra = _make_identity("archive_100", "archive_actor_100", "archive_reference", [8, 14, 20], "infra_27", [rng.random() for _ in range(6)], rng)
_IDENTITIES[_extra.identity_id] = _extra
_IDENTITIES = dict(list(_IDENTITIES.items())[:100])
_WEIGHTS = load_tuned_weights()
_AUDIT = []

SIGNAL_LABELS = {"temporal":"Temporal", "account":"Account", "infrastructure":"Infrastructure", "activity":"Activity", "interaction_network":"Interaction Network", "linguistic":"Linguistic / Stylometry"}
SIGNAL_FUNCS = {"temporal":sim.temporal_similarity, "account":sim.account_similarity, "infrastructure":sim.infrastructure_similarity, "activity":sim.activity_similarity, "interaction_network":sim.interaction_network_similarity, "linguistic":sim.linguistic_similarity}


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS investigations (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL,
        original_id TEXT, candidate_ids TEXT, result_json TEXT NOT NULL, graph_json TEXT,
        status TEXT NOT NULL DEFAULT 'completed'
    )""")
    con.commit(); con.close()

init_db()


def audit(event, details=None):
    row = {"timestamp": datetime.utcnow().isoformat()+"Z", "event": event, "details": details or {}}
    _AUDIT.append(row); return row


def _identity_summary(i):
    d = asdict(i); d["account_creation_ts"] = str(d["account_creation_ts"]); return d


def _public_identity(i, index=None):
    # Deliberately limited investigator-facing lookup fields.
    return {
        "id": i.identity_id,
        "display_name": i.handles[0] if i.handles else i.identity_id.replace("_A", "").replace("_B", "").replace("_", " ").title(),
        "handle": i.handles[0] if i.handles else i.identity_id,
        "marketplace": i.marketplaces[0] if i.marketplaces else "market_unknown",
        "domain": i.clearnet_domains[0] if i.clearnet_domains else "hidden_service",
        "wallet": i.wallets[0] if i.wallets else "wallet_redacted",
        "pgp": i.pgp_keys[0] if i.pgp_keys else "PGP unavailable",
        "record_index": index,
    }


def _parts(tag):
    return {"tls":f"tls_{tag}","service":f"svc_{tag}","certificate":f"cert_{tag}","server":f"srv_{tag}","asn":f"asn_{tag}"}


def _provenance(a,b,signal,value,detail,status):
    evidence_id=f"E-{a.identity_id}-{b.identity_id}-{signal}"
    observed_at=datetime.utcnow().isoformat()+"Z"
    payload=json.dumps({"evidence_id":evidence_id,"signal":signal,"value":value,"detail":detail,"observed_at":observed_at},sort_keys=True,default=str).encode()
    return {"evidence_id":evidence_id,"source":"synthetic_intelligence_dataset","observed_at":observed_at,"signal":signal,"value":round(value,3),"reliability":"prototype-controlled","hash":"sha256:"+hashlib.sha256(payload).hexdigest(),"status":status,"detail":detail}


def _signal_details(a,b,result):
    rows=[]
    for signal,fn in SIGNAL_FUNCS.items():
        value,detail=fn(a,b)
        status="CONTRADICTING" if signal in result.contradictory_evidence else "SUPPORTING" if signal in result.supporting_evidence else "INSUFFICIENT"
        rows.append({"signal":signal,"label":SIGNAL_LABELS[signal],"similarity":round(value,3),"weight":_WEIGHTS.get(signal,0),"contribution":result.contributions.get(signal,0),"status":status,"evidence":detail,"provenance":_provenance(a,b,signal,value,detail,status)})
    return rows


def _timeline(a,b,result):
    order=["account","temporal","activity","infrastructure","interaction_network","linguistic"]
    start=min(a.account_creation_ts,b.account_creation_ts); running=0.0; rows=[]
    for idx,signal in enumerate(order):
        value,detail=SIGNAL_FUNCS[signal](a,b); contribution=result.contributions[signal]; running+=contribution
        status="CONTRADICTING" if signal in result.contradictory_evidence else "SUPPORTING" if signal in result.supporting_evidence else "INSUFFICIENT"
        rows.append({"timestamp":(start+timedelta(days=idx)).isoformat(),"evidence_id":f"E-{a.identity_id}-{b.identity_id}-{signal}","category":SIGNAL_LABELS[signal],"description":f"{SIGNAL_LABELS[signal]} evidence incorporated into the fusion engine.","status":status,"similarity":round(value,3),"contribution":round(contribution,2),"score_after_event":round(max(0,min(100,running)),1),"detail":detail})
    return rows


def _serialize_result(a,b,result):
    return {"identity_a":a.identity_id,"identity_b":b.identity_id,"score":result.score,"confidence_band":result.confidence_band,"contributions":result.contributions,"contribution_pct":result.contribution_pct,"supporting_evidence":result.supporting_evidence,"contradictory_evidence":result.contradictory_evidence,"insufficient_evidence":result.insufficient_evidence,"signal_details":_signal_details(a,b,result),"privacy_context":result.privacy_context,"threat_actor_intelligence":sim.threat_actor_intelligence(a,b),"reasoning":result.reasoning,"methodology":{"fusion":"sum(similarity × weight), with contradiction penalty applied separately","contradiction_multiplier":CONTRADICTION_MULTIPLIER,"confidence_bands":"0–29 LOW; 30–49 INCONCLUSIVE; 50–69 MODERATE; 70–100 STRONG","score_note":"Strength of evidence, not probability of guilt or identity certainty."}}


def _save_case(mode, title, original_id, candidate_ids, result, graph=None):
    cid = "BV-" + uuid.uuid4().hex[:8].upper()
    con=db(); con.execute("INSERT INTO investigations VALUES (?,?,?,?,?,?,?,?,?)", (cid, datetime.utcnow().isoformat()+"Z", mode, title, original_id, json.dumps(candidate_ids), json.dumps(result), json.dumps(graph) if graph else None, "completed")); con.commit(); con.close()
    audit("Investigation stored", {"case_id":cid,"mode":mode}); return cid


def _case_graph(ids):
    return build_case_graph(_IDENTITIES, ids, _WEIGHTS)



def _live_identity(p, identity_id):
    """Build an investigator-supplied Identity from the original manual-entry workflow."""
    def nums(v, default):
        if isinstance(v, list):
            return [float(x) for x in v]
        if v is None or str(v).strip() == "":
            return default
        return [float(x.strip()) for x in str(v).split(",") if x.strip()]
    hours = [int(x) % 24 for x in nums(p.get("active_hours"), [12])]
    style = (nums(p.get("writing_style_vector"), [.5] * 6) + [.5] * 6)[:6]
    activity = p.get("activity_sequence", [])
    activity = [str(x).strip() for x in (activity if isinstance(activity, list) else str(activity).split(",")) if str(x).strip()]
    iv = (nums(p.get("interaction_vector"), [5,5,5,5,5]) + [5] * 5)[:5]
    counterparties = p.get("interaction_counterparties", [])
    counterparties = [str(x).strip() for x in (counterparties if isinstance(counterparties, list) else str(counterparties).split(",")) if str(x).strip()]
    try:
        created = datetime.fromisoformat(str(p.get("account_creation_ts")))
    except Exception:
        created = datetime(2026, 6, 1, 12, 0)
    infra = str(p.get("infra_fingerprint") or "unknown")
    infra_features = {k: str(p.get("infra_" + k) or f"{k}_unknown") for k in ["tls","service","certificate","server","asn"]}
    def str_list(v):
        if isinstance(v, list): return [str(x).strip() for x in v if str(x).strip()]
        return [x.strip() for x in str(v or "").split(",") if x.strip()]
    cert_domain_links = p.get("certificate_domain_links") if isinstance(p.get("certificate_domain_links"), list) else []
    return Identity(
        identity_id, f"manual_{identity_id}", hours, float(p.get("session_gap_minutes") or 45), infra, created,
        float(p.get("posting_interval_minutes") or 120), activity or ["post","browse","login"],
        bool(p.get("uses_tor")), bool(p.get("uses_vpn")), bool(p.get("uses_crypto")), bool(p.get("uses_encrypted_msg")),
        style, "manual_investigation", iv, counterparties, infra_features,
        str_list(p.get("handles")), str_list(p.get("pgp_keys")), str_list(p.get("wallets")), str_list(p.get("trust_links")),
        str_list(p.get("marketplaces")), str_list(p.get("clearnet_domains")), str_list(p.get("ssl_certificates")), cert_domain_links,
        str_list(p.get("service_banners")), str_list(p.get("exposed_server_status")), str_list(p.get("descriptor_inconsistencies")),
        str_list(p.get("tor_services")), str_list(p.get("persona_history"))
    )


def _ranked_manual(payload):
    original = _live_identity(payload["original"], "manual_original")
    results = []
    local = {original.identity_id: original}
    for idx, raw in enumerate(payload["candidates"], 1):
        ident = _live_identity(raw, f"manual_candidate_{idx}")
        local[ident.identity_id] = ident
        r = compute_actor_linkage(original, ident, _WEIGHTS)
        results.append({
            "rank": 0, "name": raw.get("name") or f"Candidate {idx}",
            "identity": _public_identity(ident), "result": _serialize_result(original, ident, r),
            "timeline": _timeline(original, ident, r), "manual_record": _identity_summary(ident)
        })
    results.sort(key=lambda x: x["result"]["score"], reverse=True)
    for n, x in enumerate(results, 1): x["rank"] = n
    graph = build_case_graph(local, [original.identity_id] + [x["identity"]["id"] for x in results], _WEIGHTS)
    manual_records = {"original": _identity_summary(original), "candidates": [x["manual_record"] for x in results]}
    return original, results, graph, manual_records


@app.post("/api/manual/investigate")
def manual_investigate(payload: dict):
    original = payload.get("original")
    candidates = payload.get("candidates", [])
    if not isinstance(original, dict) or not isinstance(candidates, list) or len(candidates) != 2:
        raise HTTPException(400, "Provide one manually entered original and exactly two candidates.")
    if not original.get("name") or not original.get("active_hours") or not original.get("infra_fingerprint"):
        raise HTTPException(400, "Original requires a name, active hours and infrastructure fingerprint.")
    for i, c in enumerate(candidates, 1):
        if not c.get("name") or not c.get("active_hours") or not c.get("infra_fingerprint"):
            raise HTTPException(400, f"Candidate {i} requires a name, active hours and infrastructure fingerprint.")
    o, results, graph, manual_records = _ranked_manual({"original": original, "candidates": candidates})
    result = {"mode":"manual", "original":_public_identity(o), "results":results, "timeline":results[0]["timeline"] if results else [], "manual_records":manual_records}
    case_id = _save_case("manual", f"Manual investigation — {original.get('name')}", "manual_original", ["manual_candidate_1","manual_candidate_2"], result, graph)
    return {"case_id":case_id, **result, "graph":graph}


def _ranked_ids(original_id, candidate_ids):
    original=_IDENTITIES.get(original_id)
    if not original: raise HTTPException(404,"Original identity not found in synthetic intelligence records.")
    results=[]
    for cid in candidate_ids:
        cand=_IDENTITIES.get(cid)
        if not cand: raise HTTPException(404,f"Candidate {cid} not found.")
        r=compute_actor_linkage(original,cand,_WEIGHTS)
        results.append({"id":cid,"identity":_public_identity(cand),"result":_serialize_result(original,cand,r),"timeline":_timeline(original,cand,r)})
    results.sort(key=lambda x:x["result"]["score"], reverse=True)
    for n,x in enumerate(results,1): x["rank"]=n
    # Graph includes original and candidates; progressively rendered by the frontend.
    graph=_case_graph([original_id]+[x["id"] for x in results])
    return original,results,graph


@app.get("/hooded-watermark.png")
def hooded_watermark():
    """Serve the investigator watermark used by the full-screen UI and active scan animation."""
    return FileResponse(ROOT / "frontend" / "hooded-watermark.png", media_type="image/png")


@app.get("/app")
def app_page():
    """Serve the full-screen investigator UI from the FastAPI server."""
    return FileResponse(ROOT / "frontend" / "index.html")


def _demo_catalog():
    by_scenario={}
    for i in _IDENTITIES.values():
        by_scenario.setdefault(i.scenario_type, []).append(i)
    def first(s, suffix=None):
        rows=by_scenario.get(s, [])
        if suffix:
            for x in rows:
                if x.identity_id.endswith(suffix): return x
        return rows[0] if rows else None
    same_a=first("same_actor_multi_identity", "_A")
    same_b=first("same_actor_multi_identity", "_B")
    noisy_a=first("noisy_behavioral", "_A")
    noisy_b=first("noisy_behavioral", "_B")
    unrelated=by_scenario.get("independent_unrelated_users", [])
    partial=by_scenario.get("partial_overlap", [])
    return {
        "match": {"label":"MATCH DEMO","description":"A controlled pair generated from the same synthetic actor, plus a distractor candidate.","expected":"STRONG correlation","original":same_a.identity_id if same_a else None,"candidates":[x.identity_id for x in [same_b, unrelated[0] if unrelated else None] if x]},
        "no_match": {"label":"NO-MATCH DEMO","description":"Independent synthetic identities with deliberately conflicting or unrelated evidence patterns.","expected":"LOW / NO STRONG CORRELATION","original":"contradict_1_B" if "contradict_1_B" in _IDENTITIES else (unrelated[0].identity_id if unrelated else None),"candidates":[x for x in ["unrelated_Y_4_A","contradict_3_A"] if x in _IDENTITIES]},
        "noisy": {"label":"NOISY MATCH","description":"A same-actor pair with behavioral noise, useful for showing imperfect but converging evidence.","expected":"MODERATE / STRONG correlation","original":"noisy_2_A" if "noisy_2_A" in _IDENTITIES else (noisy_a.identity_id if noisy_a else None),"candidates":[x for x in ["noisy_2_B","partial_6_B"] if x in _IDENTITIES]},
        "identify": {"label":"IDENTIFY DEMO","description":"Two anonymous observations generated from the same controlled actor family.","expected":"Identity correlation found","observations":[same_a.identity_id if same_a else None, same_b.identity_id if same_b else None]}
    }


@app.get("/api/demo-cases")
def demo_cases():
    catalog=_demo_catalog()
    def enrich(item):
        out=dict(item)
        for key in ("original",):
            if out.get(key) in _IDENTITIES: out[key+"_record"]=_public_identity(_IDENTITIES[out[key]])
        if "candidates" in out: out["candidate_records"]=[_public_identity(_IDENTITIES[x]) for x in out["candidates"] if x in _IDENTITIES]
        if "observations" in out: out["observation_records"]=[_public_identity(_IDENTITIES[x]) for x in out["observations"] if x in _IDENTITIES]
        return out
    return {k:enrich(v) for k,v in catalog.items()}


@app.get("/api/dataset")
def dataset():
    rows=[_public_identity(i,n+1) for n,i in enumerate(_IDENTITIES.values())]
    return {"count":len(rows),"records":rows,"underlying_fields":25,"note":"The UI exposes lookup fields; the investigation engine uses the complete synthetic record."}

@app.get("/api/dataset/{identity_id}")
def dataset_record(identity_id:str):
    i=_IDENTITIES.get(identity_id)
    if not i: raise HTTPException(404,"Identity not found")
    return {"lookup":_public_identity(i),"record":_identity_summary(i)}

@app.get("/api/investigations")
def investigations():
    con=db(); rows=con.execute("SELECT id,created_at,mode,title,original_id,candidate_ids,status FROM investigations ORDER BY created_at DESC").fetchall(); con.close()
    return {"investigations":[{**dict(r),"candidate_ids":json.loads(r["candidate_ids"])} for r in rows]}

@app.get("/api/investigations/{case_id}")
def investigation(case_id:str):
    con=db(); row=con.execute("SELECT * FROM investigations WHERE id=?",(case_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Investigation not found")
    out=dict(row); out["candidate_ids"]=json.loads(out["candidate_ids"]); out["result"]=json.loads(out["result_json"]); out["graph"]=json.loads(out["graph_json"]) if out["graph_json"] else None; del out["result_json"],out["graph_json"]; return out

@app.post("/api/investigate")
def investigate(payload:dict):
    original_id=payload.get("original_id"); candidates=payload.get("candidate_ids",[])
    if not original_id or not isinstance(candidates,list) or len(candidates)!=2: raise HTTPException(400,"Choose one original identity and exactly two candidates.")
    original,results,graph=_ranked_ids(original_id,candidates)
    case_id=_save_case("compare",f"Compare {original.handles[0] if original.handles else original.identity_id}",original_id,candidates,{"mode":"compare","original":_public_identity(original),"results":results,"timeline":results[0]["timeline"] if results else []},graph)
    return {"case_id":case_id,"mode":"compare","original":_public_identity(original),"results":results,"graph":graph}

@app.get("/api/identify/options")
def identify_options():
    # The UI presents anonymized observation labels. Underlying records remain complete.
    rows=[]
    for n,i in enumerate(_IDENTITIES.values(),1):
        p=_public_identity(i,n); p["observation_label"]=f"Observation {n:03d}"; p["source_id"]=i.identity_id; del p["display_name"]; rows.append(p)
    return {"count":len(rows),"observations":rows}

@app.post("/api/identify")
def identify(payload:dict):
    ids=payload.get("observation_ids",[])
    if not isinstance(ids,list) or len(ids)!=2: raise HTTPException(400,"Choose two observations.")
    a=_IDENTITIES.get(ids[0]); b=_IDENTITIES.get(ids[1])
    if not a or not b: raise HTTPException(404,"Observation not found")
    # Correlate both anonymous observations against actor profiles represented by the
    # synthetic library. Each actor may have multiple records (for example A/B personas).
    # The observation pair is allowed to match its own actor family through the other
    # persona record; this is what makes the controlled deanonymization demo work.
    actor_groups={}
    for rid,record in _IDENTITIES.items():
        actor_groups.setdefault(record.actor_id, []).append(record)
    actor_scores=[]
    for actor,records in actor_groups.items():
        pair_scores=[]
        best_item=None
        for record in records:
            if record.identity_id==a.identity_id:
                r1=compute_actor_linkage(a,b,_WEIGHTS)
            else:
                r1=compute_actor_linkage(a,record,_WEIGHTS)
            if record.identity_id==b.identity_id:
                r2=compute_actor_linkage(b,a,_WEIGHTS)
            else:
                r2=compute_actor_linkage(b,record,_WEIGHTS)
            combined=round((r1.score+r2.score)/2,1)
            item={"id":record.identity_id,"identity":_public_identity(record),"combined_score":combined,"observation_scores":[r1.score,r2.score],"result_a":_serialize_result(a,record if record.identity_id!=a.identity_id else b,r1),"result_b":_serialize_result(b,record if record.identity_id!=b.identity_id else a,r2)}
            pair_scores.append(item)
            if best_item is None or combined>best_item["combined_score"]: best_item=item
        actor_scores.append({"actor_id":actor,"combined_score":best_item["combined_score"],"record":best_item})
    actor_scores.sort(key=lambda x:x["combined_score"],reverse=True)
    best=actor_scores[0]
    graph=_case_graph([ids[0],ids[1],best["record"]["id"]])
    result={"mode":"identify","observations":[_public_identity(a),_public_identity(b)],"match":best["record"],"matched_actor":best["actor_id"],"actor_score":best["combined_score"],"alternatives":[x["record"] for x in actor_scores[1:5]],"note":"Synthetic-record correlation: Black Veil found the strongest actor profile represented in the controlled intelligence library. This is a prototype demonstration, not a claim about a real-world person."}
    case_id=_save_case("identify","Identify unknown observations",None,ids,result,graph)
    return {"case_id":case_id,**result,"graph":graph}

# Compatibility/demo endpoints from v1 remain available.
@app.get("/api/demo/{case}")
def get_demo(case:str):
    # Use deterministic records from the new library for demo cases.
    vals=list(_IDENTITIES.values())
    pairs={"case-a":(vals[0],vals[1]),"case-b":(vals[2],vals[3]),"case-c":(vals[4],vals[5])}
    if case not in pairs: raise HTTPException(404,"Unknown case")
    a,b=pairs[case]; r=compute_actor_linkage(a,b,_WEIGHTS); return {"label":case,"identities":[_identity_summary(a),_identity_summary(b)],"result":_serialize_result(a,b,r),"timeline":_timeline(a,b,r)}

@app.get("/api/graph/{case}")
def graph(case:str):
    vals=list(_IDENTITIES.values()); pairs={"case-a":(vals[0],vals[1]),"case-b":(vals[2],vals[3]),"case-c":(vals[4],vals[5])}
    if case not in pairs: raise HTTPException(404,"Unknown case")
    return build_case_graph(_IDENTITIES,[pairs[case][0].identity_id,pairs[case][1].identity_id],_WEIGHTS)

@app.post("/api/live/investigate")
def live_investigate(payload:dict):
    # Backward-compatible endpoint: accepts full records as before.
    original=payload.get("original"); candidates=payload.get("candidates",[])
    if not isinstance(original,dict) or not isinstance(candidates,list) or not 2<=len(candidates)<=3: raise HTTPException(400,"Provide an original and 2–3 candidates.")
    # Map by supplied identity id where possible, otherwise reject instead of silently inventing evidence.
    ids=[c.get("identity_id") or c.get("id") for c in candidates]
    oid=original.get("identity_id") or original.get("id")
    if oid in _IDENTITIES and all(x in _IDENTITIES for x in ids):
        o,rs,g=_ranked_ids(oid,ids); return {"mode":"LIVE INVESTIGATION","original":_identity_summary(o),"candidate_count":len(rs),"results":rs,"graph":g}
    raise HTTPException(400,"v2 uses the synthetic intelligence dataset. Select records from /api/dataset instead of entering 25-field records.")

@app.get("/api/report/{case_id}/json")
def report_json(case_id:str):
    row=investigation(case_id); return row

@app.get("/api/report/{case_id}/csv")
def report_csv(case_id:str):
    row=investigation(case_id); out=io.StringIO(); w=csv.writer(out); w.writerow(["signal","similarity","weight","contribution","status","evidence_id"])
    result=row["result"]
    results=result.get("results",[])
    if results:
        for x in results[0].get("result",{}).get("signal_details",[]): w.writerow([x["signal"],x["similarity"],x["weight"],x["contribution"],x["status"],x["provenance"]["evidence_id"]])
    return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename=black_veil_{case_id}.csv"})

@app.get("/api/integrity/{case}")
def integrity(case:str):
    vals=list(_IDENTITIES.values()); return {"chain":build_integrity_chain(vals[:2])}

@app.get("/api/evaluation")
def evaluation(): return evaluate_on_test_set(_WEIGHTS)
@app.get("/api/weights")
def weights(): return {"weights":_WEIGHTS,"contradiction_multiplier":CONTRADICTION_MULTIPLIER,"privacy_indicators_in_score":False,"synthetic_records":len(_IDENTITIES)}
@app.get("/api/audit")
def audit_log(): return {"entries":_AUDIT}
@app.get("/")
def root(): return {"status":"BLACK VEIL v3 API running","synthetic_records":len(_IDENTITIES),"attribution_signals":list(SIGNAL_LABELS.values()),"modes":["compare","identify","manual"],"persistent_storage":str(DB_PATH.name)}
