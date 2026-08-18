"""提效看板 —— 把 Ledger 记账渲染成自包含 HTML 面板（log 提效）。

对应内部版 build_dashboard.py 的开源精简实现：让用户直观看到"iLoop 这轮
给了多少证据、编译几次、止损几次、验收几次"——小任务价值感知的关键入口。
零第三方依赖，输出单文件 HTML。
"""

from __future__ import annotations

import html
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

from .ledger import Ledger, RoundStatus
from .evidence import EvidenceArtifact


class Dashboard:
    def __init__(self, ledger: Ledger, *, evidence: Optional[List[EvidenceArtifact]] = None) -> None:
        self.ledger = ledger
        self.evidence = evidence or []

    def metrics(self) -> dict:
        rounds = self.ledger.rounds
        ev_by_cap = Counter(e.capability for e in self.evidence)
        observed = sum(1 for e in self.evidence if e.is_observed())
        return {
            "rounds": len(rounds),
            "success": sum(1 for r in rounds if r.status == RoundStatus.SUCCESS),
            "failed": sum(1 for r in rounds if r.status == RoundStatus.FAILED),
            "evidence_total": len(self.evidence),
            "evidence_observed": observed,
            "evidence_inferred": len(self.evidence) - observed,
            "evidence_by_capability": dict(ev_by_cap),
            "traces": len(self.ledger.traces),
        }

    def render_html(self) -> str:
        m = self.metrics()
        cap_rows = "".join(
            f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
            for k, v in sorted(m["evidence_by_capability"].items())
        ) or "<tr><td colspan=2>（暂无证据）</td></tr>"
        trace_items = "".join(f"<li>{html.escape(t)}</li>" for t in self.ledger.traces[-30:])
        return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>iLoop 提效看板</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:24px;color:#1d1d1f;background:#fafafa}}
h1{{font-size:20px}} .cards{{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#fff;border-radius:12px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.06);min-width:120px}}
.card .n{{font-size:28px;font-weight:600}} .card .l{{color:#86868b;font-size:13px}}
table{{border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}}
td,th{{padding:8px 16px;border-bottom:1px solid #eee;text-align:left}}
ul{{background:#fff;border-radius:8px;padding:12px 28px;line-height:1.9}}
.foot{{color:#86868b;font-size:12px;margin-top:24px}}
</style></head><body>
<h1>【iLoop】提效看板</h1>
<div class="cards">
  <div class="card"><div class="n">{m['rounds']}</div><div class="l">总轮次</div></div>
  <div class="card"><div class="n">{m['success']}</div><div class="l">成功轮</div></div>
  <div class="card"><div class="n">{m['failed']}</div><div class="l">失败轮</div></div>
  <div class="card"><div class="n">{m['evidence_total']}</div><div class="l">证据总数</div></div>
  <div class="card"><div class="n">{m['evidence_observed']}</div><div class="l">观测证据</div></div>
  <div class="card"><div class="n">{m['evidence_inferred']}</div><div class="l">推断证据</div></div>
</div>
<h2>证据类型分布</h2>
<table><tr><th>能力</th><th>证据数</th></tr>{cap_rows}</table>
<h2>动作时间线（最近 30 条）</h2>
<ul>{trace_items or '<li>（暂无）</li>'}</ul>
<div class="foot">生成于 {time.strftime('%Y-%m-%d %H:%M:%S')} · iLoop 提效看板</div>
</body></html>"""

    def save(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.render_html(), encoding="utf-8")
        return str(p)
