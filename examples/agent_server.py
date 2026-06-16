"""Agent Server —— HTTP API + Dashboard + Cases。

端点:
  POST /api/chat/stream         SSE 流式对话
  POST /api/flush               Redis → DB 消费
  GET  /api/dashboard/summary   汇总统计
  GET  /api/dashboard/traces    Trace 列表
  GET  /api/dashboard/traces/{id} 详情（span + score）
  GET  /                         Dashboard HTML
  GET  /api/cases               用例列表
  POST /api/cases               创建用例
  GET  /api/cases/{id}          用例详情
  POST /api/cases/from-trace/{id} Trace → Case
  POST /api/cases/{id}/evaluate  单Case评分
"""

import asyncio
import atexit
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # examples/ 目录

from agent_eval_sdk import TraceReporter
from backend.core.database import async_session_factory
from backend.core.models import Trace, Span, EvalScore, EvalCase

# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

LLM_CONFIG = {
    "model": "qwen3.7-max",
    "fast_model": "qwen3.6-flash",
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

# ── OTel Agent 初始化（可选依赖，不可用时优雅降级）──────────────────────
otel_available = False
otel_agent = None
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from agent_eval_sdk.adapters.otel_exporter import EvalSpanExporter
    from example_agent import OtelExampleAgent  # noqa: E402

    provider = TracerProvider()
    otel_exporter = EvalSpanExporter(
        redis_url=REDIS_URL,
        agent_version="example-otel-v1.0.0",
        source="production",
    )
    # SimpleSpanProcessor：每个 Span 结束时立即触发 export()。
    # EvalSpanExporter 内部会缓存子 Span，等根 Span 到达后统一输出完整 trace。
    provider.add_span_processor(SimpleSpanProcessor(otel_exporter))
    trace.set_tracer_provider(provider)

    otel_agent = OtelExampleAgent(**LLM_CONFIG)
    otel_available = True
    print(f"✅ OTel Agent 就绪（SimpleSpanProcessor → EvalSpanExporter）")

    # 进程退出时优雅关闭 OTel 资源（释放 Redis 连接）
    def _shutdown_otel():
        try:
            otel_exporter.shutdown()
        except Exception:
            pass
    atexit.register(_shutdown_otel)

except ImportError as e:
    print(f"⚠️  OTel Agent 不可用（缺少依赖: {e}），请安装: pip install agent-eval-sdk[otel]")
except Exception as e:
    print(f"⚠️  OTel Agent 初始化失败: {e}")

_executor = ThreadPoolExecutor(max_workers=4)

app = FastAPI(title="Agent Eval", version="2.0.0")

# ── 注册 cases 路由（Trace→Case + 单Case评分）──────────────────────
from backend.api.cases import router as cases_router
app.include_router(cases_router)


# ═══════════════════════════════════════════════════════════════════════════
# SSE 流式对话
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """SSE 流式对话 —— 逐 token 推送到前端。

    支持 trace_mode 参数选择埋点方式：
    - "sdk"（默认）：SDK 显式埋点（TraceReporter）
    - "otel"：OTel 自动埋点（EvalSpanExporter），需安装 agent-eval-sdk[otel]
    """
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "query 不能为空"}, 400)

    trace_mode = (body.get("trace_mode") or "sdk").strip().lower()

    # 选择 Agent
    if trace_mode == "otel" and otel_available:
        selected_agent = otel_agent
    else:
        selected_agent = agent

    run_id = str(uuid.uuid4())

    async def event_stream() -> AsyncGenerator[str, None]:
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def producer():
            """在独立线程中运行同步生成器，将 token 推入 asyncio 队列。"""
            try:
                for token in selected_agent.run_stream_tokens(query, run_id):
                    loop.call_soon_threadsafe(queue.put_nowait, token)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, f"[error] {e}")
            # sentinel：通知消费者结束
            loop.call_soon_threadsafe(queue.put_nowait, None)

        _executor.submit(producer)

        # 先发 run_id
        yield f"data: {_json.dumps({'type': 'meta', 'run_id': run_id})}\n\n"

        while True:
            token = await queue.get()
            if token is None:
                break
            if token.startswith("[status]"):
                yield f"data: {_json.dumps({'type': 'status', 'text': token[8:]})}\n\n"
            elif token.startswith("[tool]"):
                yield f"data: {_json.dumps({'type': 'tool', 'text': token[6:]})}\n\n"
            elif token.startswith("[error]"):
                yield f"data: {_json.dumps({'type': 'error', 'text': token[7:]})}\n\n"
            else:
                yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"

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

        # 查询哪些 trace 已转为 case
        trace_ids = [t.id for t in traces]
        trace_to_case: dict = {}  # trace_id → case_id
        if trace_ids:
            cs_result = await s.execute(
                select(EvalCase.source_trace_id, EvalCase.id).where(
                    EvalCase.source_trace_id.in_(trace_ids)
                )
            )
            for row in cs_result.all():
                trace_to_case[str(row[0])] = str(row[1])

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
                "already_case": str(t.id) in trace_to_case,
                "case_id": trace_to_case.get(str(t.id)),
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
# HTML —— 纯 Chat 对话页面（看板已迁移到 18000）
# ═══════════════════════════════════════════════════════════════════════════

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Agent Eval · Chat</title>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--text:#e1e4eb;--muted:#888ca0;
  --accent:#6c8aff;--green:#4ade80;--red:#f87171;--purple:#a78bfa;}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow:hidden}
.topbar{display:flex;justify-content:space-between;align-items:center;padding:12px 24px;background:var(--card);border-bottom:1px solid var(--border)}
.topbar h1{font-size:18px;display:flex;align-items:center;gap:8px}
.topbar a{color:var(--accent);text-decoration:none;font-size:13px;padding:6px 14px;border:1px solid var(--accent);border-radius:6px;transition:.2s}
.topbar a:hover{background:var(--accent);color:#fff}
.btn-flush{padding:6px 14px;background:var(--green);color:#000;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;margin-right:10px}
.btn-flush:disabled{opacity:.4}
.mode-switch{display:flex;gap:2px;background:var(--bg);border-radius:8px;padding:3px;margin-right:12px}
.mode-option{padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;color:var(--muted);transition:.2s;user-select:none}
.mode-option input{display:none}
.mode-option.active{background:var(--accent);color:#fff;font-weight:600}
.mode-option.disabled{opacity:.3;cursor:not-allowed}
.chat-wrap{flex:1;overflow:hidden;display:flex;flex-direction:column;height:calc(100vh - 60px)}
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
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.cursor{display:inline-block;width:8px;height:18px;background:var(--accent);animation:blink .8s infinite;vertical-align:text-bottom;margin-left:2px}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
</style>
</head>
<body>

<div class="topbar">
  <h1>💬 Agent Eval · Chat</h1>
  <div style="display:flex;align-items:center;">
    <div class="mode-switch">
      <label class="mode-option active" id="modeSdk" onclick="switchMode('sdk')">
        <input type="radio" name="traceMode" value="sdk" checked> SDK 埋点
      </label>
      <label class="mode-option" id="modeOtel" onclick="switchMode('otel')">
        <input type="radio" name="traceMode" value="otel"> OTel 埋点
      </label>
    </div>
    <button class="btn-flush" onclick="flushEvents()">📥 Flush</button>
    <a href="http://localhost:18000/dashboard/" target="_blank">📊 查看看板 →</a>
  </div>
</div>

<div class="chat-wrap">
  <div class="chat-msgs" id="chatMsgs">
    <div class="msg agent"><div class="bubble">👋 你好！我是支持工具调用 + 流式输出的 ExampleAgent。</div></div>
  </div>
  <div class="chat-bar">
    <input id="chatInput" placeholder="输入问题..." onkeydown="if(event.key==='Enter')send()">
    <button id="sendBtn" onclick="send()">发送</button>
  </div>
</div>

<script>
let currentBubble=null;
let currentTraceMode='sdk';

function switchMode(mode){
  currentTraceMode=mode;
  document.getElementById('modeSdk').classList.toggle('active',mode==='sdk');
  document.getElementById('modeOtel').classList.toggle('active',mode==='otel');
}

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
      headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q,trace_mode:currentTraceMode})});
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
async function flushEvents(){
  const btn=document.querySelector('.btn-flush');btn.disabled=true;btn.textContent='⏳...';
  try{const r=await fetch('/api/flush',{method:'POST'});const d=await r.json();
    alert('Flush: '+d.batches+' 批, 剩余 '+d.remaining);}
  catch(e){alert('失败: '+e.message);}
  btn.disabled=false;btn.textContent='📥 Flush';
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return CHAT_HTML


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  🚀 Agent Eval v2 · 流式对话 + Dashboard")
    print(f"  📡 http://localhost:8800")
    print(f"  🔧 {LLM_CONFIG['model']}")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8800, log_level="info", access_log=False)
