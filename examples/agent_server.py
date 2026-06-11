"""Agent Server —— HTTP API + Dashboard。

端点:
  POST /api/chat/stream      SSE 流式对话
  POST /api/flush             Redis → DB 消费
  GET  /api/dashboard/summary 汇总统计
  GET  /api/dashboard/traces  Trace 列表
  GET  /api/dashboard/traces/{id}  详情（span + score）
  GET  /                        Dashboard HTML
"""

import asyncio
import json as _json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select, func, desc

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent_eval_sdk import TraceReporter
from backend.core.database import async_session_factory
from backend.core.models import Trace, Span, EvalScore

# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

LLM_CONFIG = {
    "model": "qwen3.7-plus",
    "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "api_key": os.environ.get(
        "DASHSCOPE_API_KEY",
        "sk-ws-H.HYXRDH.Pfz9.MEMCHxknGBxxfv-ymjc6Y-QPJuZhiNz9hioGE2Cq5qAZsAoCIHXpJPDBB7PqdQSAbbbGVD3iCQRfgqcalASLdpF0_E4N",
    ),
}
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ── Init ─────────────────────────────────────────────────────────────────

try:
    reporter = TraceReporter(agent_version="example-v1.0.0", redis_url=REDIS_URL)
    reporter._redis.ping()
    print(f"✅ Redis OK ({REDIS_URL})")
except Exception as e:
    print(f"⚠️  Redis 不可用 ({e})")
    reporter = None

from example_agent import ExampleAgent  # noqa: E402

agent = ExampleAgent(reporter=reporter, **LLM_CONFIG)
_executor = ThreadPoolExecutor(max_workers=4)

app = FastAPI(title="Agent Eval", version="2.0.0")


# ═══════════════════════════════════════════════════════════════════════════
# SSE 流式对话
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """SSE 流式对话 —— 逐 token 推送到前端。"""
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "query 不能为空"}, 400)

    run_id = str(uuid.uuid4())

    async def event_stream() -> AsyncGenerator[str, None]:
        loop = asyncio.get_event_loop()
        gen = await loop.run_in_executor(
            _executor, lambda: agent.run_stream_tokens(query, run_id)
        )
        # 先发 run_id
        yield f"data: {_json.dumps({'type': 'meta', 'run_id': run_id})}\n\n"

        try:
            for token in gen:
                if token.startswith("[status]"):
                    yield f"data: {_json.dumps({'type': 'status', 'text': token[8:]})}\n\n"
                elif token.startswith("[tool]"):
                    yield f"data: {_json.dumps({'type': 'tool', 'text': token[6:]})}\n\n"
                elif token.startswith("[error]"):
                    yield f"data: {_json.dumps({'type': 'error', 'text': token[7:]})}\n\n"
                else:
                    yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type': 'error', 'text': str(e)})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Flush
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/flush")
async def api_flush():
    if reporter is None:
        return {"status": "skipped", "reason": "Redis 不可用"}
    import redis.asyncio as aioredis
    from backend.workers.ingest_worker import IngestWorker

    worker = IngestWorker()
    worker._redis = aioredis.from_url(REDIS_URL)
    consumed = 0
    for _ in range(100):
        if await worker._redis.llen(worker._span_key) == 0:
            break
        try:
            await worker._consume_batch()
            consumed += 1
        except Exception:
            break
    remaining = await worker._redis.llen(worker._span_key)
    await worker._redis.aclose()
    return {"status": "ok", "batches": consumed, "remaining": remaining}


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard API
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/dashboard/summary")
async def dashboard_summary():
    async with async_session_factory() as s:
        total = await s.scalar(select(func.count(Trace.id)))
        success = await s.scalar(select(func.count(Trace.id)).where(Trace.status == "success"))
        avg_score = await s.scalar(select(func.avg(Trace.overall_score)))
        total_spans = await s.scalar(select(func.count(Span.id)))
        total_scores = await s.scalar(select(func.count(EvalScore.id)))
    return {
        "total_traces": total or 0,
        "success_count": success or 0,
        "error_count": (total or 0) - (success or 0),
        "avg_overall_score": round(float(avg_score), 2) if avg_score else None,
        "total_spans": total_spans or 0,
        "total_scores": total_scores or 0,
    }


@app.get("/api/dashboard/traces")
async def dashboard_traces(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    async with async_session_factory() as s:
        total = await s.scalar(select(func.count(Trace.id)))
        rows = await s.execute(
            select(Trace).order_by(desc(Trace.created_at)).limit(limit).offset(offset)
        )
        traces = rows.scalars().all()
    return {
        "total": total,
        "traces": [
            {
                "id": str(t.id),
                "query": (t.query or "")[:80],
                "status": t.status,
                "source": t.source,
                "overall_score": float(t.overall_score) if t.overall_score else None,
                "total_latency_ms": t.total_latency_ms,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "span_count": None,
            }
            for t in traces
        ],
    }


@app.get("/api/dashboard/traces/{trace_id}")
async def dashboard_trace_detail(trace_id: str):
    try:
        tid = uuid.UUID(trace_id)
    except ValueError:
        return JSONResponse({"error": "非法 trace_id"}, 400)

    async with async_session_factory() as s:
        trace = await s.get(Trace, tid)
        if not trace:
            return JSONResponse({"error": "不存在"}, 404)

        spans_r = await s.execute(
            select(Span).where(Span.trace_id == tid).order_by(Span.sequence)
        )
        spans = spans_r.scalars().all()
        scores_r = await s.execute(select(EvalScore).where(EvalScore.trace_id == tid))
        scores = scores_r.scalars().all()

        # 按 span_type 统计
        type_counts = {}
        for sp in spans:
            type_counts[sp.span_type] = type_counts.get(sp.span_type, 0) + 1

    return {
        "trace": {
            "id": str(trace.id),
            "query": trace.query,
            "status": trace.status,
            "source": trace.source,
            "final_response": trace.final_response,
            "overall_score": float(trace.overall_score) if trace.overall_score else None,
            "total_latency_ms": trace.total_latency_ms,
            "total_tokens": trace.total_tokens,
            "created_at": trace.created_at.isoformat() if trace.created_at else None,
            "span_count": len(spans),
            "span_distribution": type_counts,
        },
        "spans": [
            {
                "id": str(sp.id),
                "span_type": sp.span_type,
                "sequence": sp.sequence,
                "input": sp.input,
                "output": sp.output,
                "latency_ms": sp.latency_ms,
                "tokens": sp.tokens,
                "model": sp.model,
                "score": float(sp.score) if sp.score else None,
                "tool_name": sp.tool_name,
                "tool_params": sp.tool_params,
                "tool_result": sp.tool_result,
            }
            for sp in spans
        ],
        "eval_scores": [
            {
                "id": str(sc.id),
                "span_id": str(sc.span_id) if sc.span_id else None,
                "score": float(sc.score),
                "metrics": sc.metrics,
                "method": sc.method,
            }
            for sc in scores
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Agent Eval · Chat + Dashboard</title>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e1e4eb;--muted:#888ca0;
  --accent:#6c8aff;--green:#4ade80;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa;}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow:hidden}
.tabs{display:flex;gap:2px;padding:16px 24px 0;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
.tab{padding:10px 20px;border:none;background:none;color:var(--muted);cursor:pointer;font-size:15px;border-radius:8px 8px 0 0;transition:.2s}
.tab.active{background:var(--bg);color:var(--accent)}.tab:hover:not(.active){color:var(--text)}
.panel{display:none;max-width:1200px;margin:0 auto}.panel.active{display:flex;flex-direction:column;height:calc(100vh - 64px)}
/* Chat */
.chat-wrap{flex:1;overflow:hidden;display:flex;flex-direction:column}
.chat-msgs{flex:1;overflow-y:auto;padding:20px 24px}
.msg{margin-bottom:18px;animation:fadeIn .3s}
.msg.user{text-align:right}.msg.agent{text-align:left}
.msg .bubble{display:inline-block;max-width:80%;padding:12px 16px;border-radius:16px;line-height:1.6;white-space:pre-wrap;word-break:break-word;text-align:left}
.msg.user .bubble{background:var(--accent);color:#fff;border-bottom-right-radius:4px}
.msg.agent .bubble{background:var(--border);border-bottom-left-radius:4px}
.msg .tag{font-size:10px;color:var(--muted);margin-top:4px}
.status-tag{display:inline-block;padding:4px 10px;margin:6px 0;border-radius:6px;font-size:12px;color:var(--muted);background:rgba(136,140,160,.1);animation:fadeIn .3s}
.tool-tag{display:inline-block;padding:4px 10px;margin:6px 0;border-radius:6px;font-size:12px;color:var(--purple);background:rgba(167,139,250,.1);animation:fadeIn .3s}
.chat-bar{display:flex;gap:12px;padding:16px 24px;background:var(--card);border-top:1px solid var(--border)}
.chat-bar input{flex:1;padding:14px 18px;background:var(--bg);border:1px solid var(--border);border-radius:12px;color:var(--text);font-size:15px;outline:none}
.chat-bar input:focus{border-color:var(--accent)}
.chat-bar button{padding:14px 28px;background:var(--accent);color:#fff;border:none;border-radius:12px;cursor:pointer;font-size:15px;font-weight:600}
.chat-bar button:hover{opacity:.85}.chat-bar button:disabled{opacity:.4;cursor:not-allowed}
/* Dashboard */
.dash-wrap{padding:24px;overflow-y:auto;height:calc(100vh - 64px)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--card);border-radius:12px;padding:18px;border:1px solid var(--border)}
.stat-card .label{font-size:12px;color:var(--muted);margin-bottom:6px}
.stat-card .value{font-size:26px;font-weight:700}
.value.green{color:var(--green)}.value.red{color:var(--red)}
.dash-bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.dash-bar h2{font-size:18px}
.dash-bar button{padding:8px 16px;background:var(--border);color:var(--text);border:none;border-radius:8px;cursor:pointer;font-size:13px;margin-left:8px;transition:.2s}
.dash-bar button:hover{background:var(--accent)}.dash-bar button.active{background:var(--accent)}
.btn-flush{padding:8px 16px;background:var(--green)!important;color:#000!important;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
.btn-flush:disabled{opacity:.4}
.trace-table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}
.trace-table th,.trace-table td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);font-size:13px}
.trace-table th{color:var(--muted);font-weight:600;font-size:12px}
.trace-table tr{cursor:pointer;transition:.2s}.trace-table tr:hover{background:rgba(108,138,255,.06)}
.badge{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600}
.badge.success{background:rgba(74,222,128,.15);color:var(--green)}.badge.error{background:rgba(248,113,113,.15);color:var(--red)}
/* Side Panel */
.overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:100;opacity:0;pointer-events:none;transition:opacity .3s}
.overlay.show{opacity:1;pointer-events:auto}
.sidepanel{position:fixed;top:0;right:-560px;width:540px;height:100%;background:var(--card);z-index:101;transition:right .35s ease;display:flex;flex-direction:column;box-shadow:-4px 0 24px rgba(0,0,0,.4)}
.sidepanel.show{right:0}
.sp-header{display:flex;justify-content:space-between;align-items:center;padding:20px 24px;border-bottom:1px solid var(--border)}
.sp-header h3{font-size:17px}.sp-header button{background:none;border:none;color:var(--muted);cursor:pointer;font-size:20px;padding:4px 8px}
.sp-tabs{display:flex;border-bottom:1px solid var(--border);padding:0 24px}
.sp-tab{padding:10px 18px;border:none;background:none;color:var(--muted);cursor:pointer;font-size:14px;border-bottom:2px solid transparent;transition:.2s}
.sp-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.sp-body{flex:1;overflow-y:auto;padding:20px 24px}
.sp-body pre{background:var(--bg);border-radius:8px;padding:16px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;max-height:calc(100vh - 300px);overflow:auto}
.span-card{background:var(--bg);border-radius:8px;padding:14px;border:1px solid var(--border);margin-bottom:10px}
.span-card .s-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.span-card .s-type{font-weight:700;color:var(--accent);font-size:14px}
.span-card .s-meta{font-size:11px;color:var(--muted)}
.span-card .s-body{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px}
.span-card .s-body pre{margin:4px 0;max-height:100px;overflow:auto;padding:8px;font-size:11px}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.empty{text-align:center;padding:60px;color:var(--muted)}
.cursor{display:inline-block;width:8px;height:18px;background:var(--accent);animation:blink .8s infinite;vertical-align:text-bottom;margin-left:2px}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
</style>
</head>
<body>

<div class="tabs">
  <button class="tab active" id="tabChat" onclick="switchTab('chat')">💬 Chat</button>
  <button class="tab" id="tabDash" onclick="switchTab('dashboard')">📊 Dashboard</button>
</div>

<!-- ═══════════════ Chat ═══════════════ -->
<div id="panel-chat" class="panel active">
  <div class="chat-wrap">
    <div class="chat-msgs" id="chatMsgs">
      <div class="msg agent"><div class="bubble">👋 你好！我是支持工具调用 + 流式输出的 ExampleAgent。</div></div>
    </div>
    <div class="chat-bar">
      <input id="chatInput" placeholder="输入问题（支持加减法计算）..." onkeydown="if(event.key==='Enter')send()">
      <button id="sendBtn" onclick="send()">发送</button>
    </div>
  </div>
</div>

<!-- ═══════════════ Dashboard ═══════════════ -->
<div id="panel-dashboard" class="panel">
  <div class="dash-wrap">
    <div class="dash-bar"><h2>📊 评测总览</h2>
      <div>
        <button id="autoBtn" onclick="toggleAuto()">🔄 自动刷新: 关</button>
        <button class="btn-flush" onclick="flushEvents()">📥 Flush</button>
      </div>
    </div>
    <div class="stats" id="statsCards"></div>
    <h3 style="color:var(--muted);margin-bottom:10px">📋 最近 Trace</h3>
    <div id="tracesTable"></div>
  </div>
</div>

<!-- ═══════════════ Side Panel ═══════════════ -->
<div class="overlay" id="overlay" onclick="closePanel()"></div>
<div class="sidepanel" id="sidepanel">
  <div class="sp-header">
    <h3>🔍 Trace 详情</h3>
    <button onclick="closePanel()">✕</button>
  </div>
  <div class="sp-tabs">
    <button class="sp-tab active" id="spTabTrace" onclick="switchSpTab('trace')">📄 Trace</button>
    <button class="sp-tab" id="spTabSpans" onclick="switchSpTab('spans')">📊 Spans</button>
    <button class="sp-tab" id="spTabScores" onclick="switchSpTab('scores')">⭐ Scores</button>
  </div>
  <div class="sp-body" id="spBody"></div>
</div>

<script>
// ── Tab Switching ───────────────────────────────────────────────────
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById(name==='chat'?'tabChat':'tabDash').classList.add('active');
  document.getElementById('panel-'+name).classList.add('active');
  if(name==='dashboard'){loadDashboard();}
}
// ── Chat (SSE Streaming) ────────────────────────────────────────────
let currentBubble=null;

async function send(){
  const input=document.getElementById('chatInput');
  const q=input.value.trim(); if(!q)return;
  const btn=document.getElementById('sendBtn');
  btn.disabled=true;btn.textContent='...';
  appendMsg('user',q);
  input.value='';

  const bubble=createMsgBubble('agent');
  currentBubble=bubble;
  let fullText='';

  try{
    const resp=await fetch('/api/chat/stream',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})});
    const reader=resp.body.getReader();
    const dec=new TextDecoder();
    let buf='';

    while(true){
      const{value,done}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');
      buf=lines.pop()||'';

      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const payload=line.slice(6);
        if(payload==='[DONE]'){removeCursor();break;}
        try{
          const msg=JSON.parse(payload);
          if(msg.type==='token'){
            fullText+=msg.text;
            bubble.textContent=fullText;
            addCursor(bubble);
          }else if(msg.type==='status'){
            const el=document.createElement('div');
            el.className='status-tag';el.textContent=msg.text;
            document.getElementById('chatMsgs').appendChild(el);
            el.scrollIntoView({behavior:'smooth'});
          }else if(msg.type==='tool'){
            const el=document.createElement('div');
            el.className='tool-tag';el.textContent=msg.text;
            document.getElementById('chatMsgs').appendChild(el);
            el.scrollIntoView({behavior:'smooth'});
          }else if(msg.type==='meta'){
            const tag=document.createElement('div');
            tag.className='tag';tag.textContent='run: '+msg.run_id.slice(0,8)+'...';
            document.getElementById('chatMsgs').appendChild(tag);
          }else if(msg.type==='error'){
            bubble.textContent='❌ '+msg.text;
          }
        }catch(e){}
      }
    }
  }catch(e){
    if(currentBubble)currentBubble.textContent='❌ 网络错误: '+e.message;
  }
  removeCursor();
  currentBubble=null;
  btn.disabled=false;btn.textContent='发送';
  input.focus();
}

function createMsgBubble(role){
  const wrap=document.createElement('div');wrap.className='msg '+role;
  const bubble=document.createElement('div');bubble.className='bubble';
  wrap.appendChild(bubble);
  document.getElementById('chatMsgs').appendChild(wrap);
  wrap.scrollIntoView({behavior:'smooth'});
  return bubble;
}
function addCursor(el){
  const existing=el.parentElement?.querySelector('.cursor');
  if(existing)return;
  const c=document.createElement('span');c.className='cursor';
  el.parentElement?.appendChild(c);
}
function removeCursor(){
  document.querySelectorAll('.cursor').forEach(c=>c.remove());
}
function appendMsg(role,text){
  const wrap=document.createElement('div');wrap.className='msg '+role;
  const bubble=document.createElement('div');bubble.className='bubble';bubble.textContent=text;
  wrap.appendChild(bubble);
  document.getElementById('chatMsgs').appendChild(wrap);
  wrap.scrollIntoView({behavior:'smooth'});
}
// ── Dashboard ───────────────────────────────────────────────────────
let autoTimer=null;
function toggleAuto(){
  const btn=document.getElementById('autoBtn');
  if(autoTimer){clearInterval(autoTimer);autoTimer=null;btn.textContent='🔄 自动刷新: 关';btn.classList.remove('active');}
  else{autoTimer=setInterval(loadDashboard,10000);btn.textContent='🔄 自动刷新: 开 (10s)';btn.classList.add('active');}
}
async function loadDashboard(){
  try{
    const[sum,traces]=await Promise.all([
      fetch('/api/dashboard/summary').then(r=>r.json()),
      fetch('/api/dashboard/traces?limit=30').then(r=>r.json())]);
    document.getElementById('statsCards').innerHTML=
      `<div class="stat-card"><div class="label">Trace 总数</div><div class="value">${sum.total_traces}</div></div>`+
      `<div class="stat-card"><div class="label">成功</div><div class="value green">${sum.success_count}</div></div>`+
      `<div class="stat-card"><div class="label">失败</div><div class="value red">${sum.error_count}</div></div>`+
      `<div class="stat-card"><div class="label">平均总分</div><div class="value">${sum.avg_overall_score??'—'}</div></div>`+
      `<div class="stat-card"><div class="label">Span 总数</div><div class="value">${sum.total_spans}</div></div>`+
      `<div class="stat-card"><div class="label">评分数</div><div class="value">${sum.total_scores}</div></div>`;
    const list=traces.traces||[];
    if(!list.length){document.getElementById('tracesTable').innerHTML='<div class="empty">暂无 Trace</div>';return;}
    let h='<table class="trace-table"><thead><tr><th>时间</th><th>查询</th><th>状态</th><th>总分</th><th>延迟</th></tr></thead><tbody>';
    list.forEach(t=>{
      const time=t.created_at?new Date(t.created_at).toLocaleString('zh-CN'):'—';
      const sc=t.overall_score!=null?t.overall_score.toFixed(1):'—';
      const scClr=t.overall_score>=80?'var(--green)':t.overall_score>=60?'var(--yellow)':'var(--red)';
      h+=`<tr onclick="openPanel('${t.id}')"><td style="font-size:11px;color:var(--muted);white-space:nowrap">${time}</td>
        <td>${esc(t.query)}</td>
        <td><span class="badge ${t.status==='success'?'success':'error'}">${t.status}</span></td>
        <td style="color:${scClr};font-weight:700">${sc}</td>
        <td style="color:var(--muted)">${t.total_latency_ms?t.total_latency_ms+'ms':'—'}</td></tr>`;
    });
    h+='</tbody></table>';
    document.getElementById('tracesTable').innerHTML=h;
  }catch(e){console.error(e);}
}
// ── Side Panel ──────────────────────────────────────────────────────
let _detailCache=null;
async function openPanel(tid){
  document.getElementById('overlay').classList.add('show');
  document.getElementById('sidepanel').classList.add('show');
  document.getElementById('spBody').innerHTML='<div class="empty">⏳ 加载中...</div>';
  try{
    const r=await fetch('/api/dashboard/traces/'+tid);
    _detailCache=await r.json();
    if(_detailCache.error){document.getElementById('spBody').innerHTML='<div class="empty">❌ '+_detailCache.error+'</div>';return;}
    renderSpTab('trace');
  }catch(e){document.getElementById('spBody').innerHTML='<div class="empty">❌ '+e.message+'</div>';}
}
function closePanel(){
  document.getElementById('overlay').classList.remove('show');
  document.getElementById('sidepanel').classList.remove('show');
}
function switchSpTab(name){
  document.querySelectorAll('.sp-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('spTab'+name.charAt(0).toUpperCase()+name.slice(1)).classList.add('active');
  renderSpTab(name);
}
function renderSpTab(name){
  const d=_detailCache; if(!d)return;
  const body=document.getElementById('spBody');
  if(name==='trace'){
    const t=d.trace;
    body.innerHTML=`<pre>${JSON.stringify({query:t.query,status:t.status,source:t.source,
      overall_score:t.overall_score,total_latency_ms:t.total_latency_ms,
      total_tokens:t.total_tokens,span_count:t.span_count,
      span_distribution:t.span_distribution,created_at:t.created_at,
      final_response:t.final_response},null,2)}</pre>`;
  }else if(name==='spans'){
    const spans=d.spans||[];
    if(!spans.length){body.innerHTML='<div class="empty">暂无 Span</div>';return;}
    body.innerHTML=spans.map(s=>`<div class="span-card">
      <div class="s-head"><span class="s-type">[${s.sequence}] ${s.span_type}</span>
        <span class="s-meta">${s.latency_ms||'—'}ms | ${s.model||'—'} | score:${s.score!=null?s.score:'—'}
        ${s.tool_name?' | 🔧 '+s.tool_name:''}</span></div>
      <div class="s-body">
        <div><b style="color:var(--muted)">Input</b><pre>${JSON.stringify(s.input,null,2)}</pre></div>
        <div><b style="color:var(--muted)">Output</b><pre>${JSON.stringify(s.output,null,2)}</pre></div>
        ${s.tool_params?`<div><b style="color:var(--muted)">Tool Params</b><pre>${JSON.stringify(s.tool_params,null,2)}</pre></div>`:''}
        ${s.tool_result?`<div><b style="color:var(--muted)">Tool Result</b><pre>${JSON.stringify(s.tool_result,null,2)}</pre></div>`:''}
      </div></div>`).join('');
  }else if(name==='scores'){
    const scores=d.eval_scores||[];
    body.innerHTML='<pre>'+JSON.stringify(scores,null,2)+'</pre>';
  }
}
async function flushEvents(){
  const btn=document.querySelector('.btn-flush');btn.disabled=true;btn.textContent='⏳...';
  try{const r=await fetch('/api/flush',{method:'POST'});const d=await r.json();
    alert('Flush: '+d.batches+' 批, 剩余 '+d.remaining);loadDashboard();}
  catch(e){alert('失败: '+e.message);}
  btn.disabled=false;btn.textContent='📥 Flush';
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  🚀 Agent Eval v2 · 流式对话 + Dashboard")
    print(f"  📡 http://localhost:8800")
    print(f"  🔧 {LLM_CONFIG['model']}")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8800, log_level="info", access_log=False)
