"""
weekly_report.py
Every Sunday: writes a markdown report into /reports so you can watch the
system think - best posts, best angles, best hours, and what it will do
differently next week. Zero action required from you.
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = json.loads((ROOT / "data/post_history.json").read_text())
PERF = json.loads((ROOT / "data/performance.json").read_text())
LEADS_FILE = ROOT / "data/leads.json"
LEADS = json.loads(LEADS_FILE.read_text()) if LEADS_FILE.exists() else {"leads": []}

now = datetime.now(timezone.utc)
week_ago = (now - timedelta(days=7)).timestamp()
week_posts = [p for p in HISTORY["posts"] if p["timestamp"] >= week_ago]
scored = [p for p in week_posts if p.get("score") is not None]


def fmt_post(p):
    text = p["text"].replace("\n", " ")
    if len(text) > 140:
        text = text[:137] + "..."
    return f"> {text}\n>\n> angle: `{p['angle']}` | score: **{p.get('score', '?')}**"


lines = [f"# Weekly Autopilot Report - {now.strftime('%Y-%m-%d')}", ""]
lines.append(f"Posts published this week: **{len(week_posts)}**")
lines.append("")

if scored:
    best = sorted(scored, key=lambda x: x["score"], reverse=True)
    lines.append("## Best post of the week")
    lines.append(fmt_post(best[0]))
    lines.append("")
    if len(best) > 2:
        lines.append("## Runner-ups")
        for p in best[1:3]:
            lines.append(fmt_post(p))
            lines.append("")

angles = PERF.get("angle_scores", {})
if angles:
    ranked = sorted(angles.items(), key=lambda x: x[1]["avg_score"], reverse=True)
    lines.append("## Angle leaderboard (all-time)")
    for a, s in ranked:
        lines.append(f"- `{a}` - avg score {s['avg_score']} over {s['n']} posts")
    lines.append("")
    lines.append(f"Next week the AI will lean harder into `{ranked[0][0]}` "
                 f"style posts while still experimenting 20% of the time.")
    lines.append("")

hours = PERF.get("hour_scores", {})
if hours:
    top_hours = sorted(hours.items(), key=lambda x: x[1], reverse=True)[:3]
    lines.append("## Best posting hours (local time)")
    for h, s in top_hours:
        lines.append(f"- {h}:00 - avg score {s}")
    lines.append("")

# --- v2: conversion funnel ---
week_leads = [l for l in LEADS["leads"]
              if l.get("timestamp", "") >= (now - timedelta(days=7)).isoformat()]
lines.append("## Conversion funnel (v2)")
lines.append(f"- New leads this week (keyword comments answered): **{len(week_leads)}**")
lines.append(f"- Total leads captured all-time: **{len(LEADS['leads'])}**")
if week_leads:
    lines.append("- Newest leads: " + ", ".join(f"@{l['username']}" for l in week_leads[-8:]))
lines.append("")

fh = PERF.get("follower_history", [])
if len(fh) >= 2:
    delta = fh[-1]["count"] - fh[0]["count"]
    span = min(len(fh), 90)
    sign = "+" if delta >= 0 else ""
    lines.append("## Follower growth")
    lines.append(f"- Now: **{fh[-1]['count']}** ({sign}{delta} over the last {span} tracked days)")
    lines.append("")

ctas = PERF.get("cta_scores", {})
if ctas:
    ranked_cta = sorted(ctas.items(), key=lambda x: x[1]["avg_score"], reverse=True)
    lines.append("## CTA leaderboard")
    for c, s in ranked_cta:
        lines.append(f"- \"{c}\" - avg score {s['avg_score']} over {s['n']} posts")
    lines.append(f"\nThe AI will now favor: \"{ranked_cta[0][0]}\"")
    lines.append("")

lines.append("---")
lines.append("*This report was generated automatically. No humans were involved.*")

out = ROOT / "reports" / f"report-{now.strftime('%Y-%m-%d')}.md"
out.write_text("\n".join(lines))
print(f"Report written: {out.name}")
