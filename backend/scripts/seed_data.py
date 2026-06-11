"""Seed 数据脚本 —— 插入 5 条 eval_cases + 1 个 smoke case_set。

用法:
    cd backend && python -m scripts.seed_data
"""

import asyncio
import uuid
from datetime import datetime

from backend.core.database import async_session_factory, engine, Base
from backend.core.models import CaseSet, CaseSetMember, EvalCase


SEED_CASES = [
    {
        "query": "今天北京天气怎么样？",
        "context": {"user_location": "北京", "timezone": "Asia/Shanghai"},
        "expected_intent": {
            "intents": ["weather_query"],
            "mode": "any",
        },
        "expected_retrieval": {
            "doc_ids": ["weather_beijing_20250101", "aqi_beijing_20250101"],
            "min_precision": 0.8,
        },
        "expected_tools": [
            {"tool_name": "get_weather", "params": {"city": "北京"}},
        ],
        "expected_answer": {
            "key_phrases": ["晴", "温度", "风力"],
            "max_length": 200,
        },
        "gold_answer": "北京今天晴，气温 -5°C ~ 3°C，北风3-4级。",
        "difficulty": "easy",
        "category": "weather",
        "tags": ["weather", "retrieval"],
    },
    {
        "query": "帮我计算 (123 + 456) * 78 / 3 等于多少？",
        "context": {},
        "expected_intent": {
            "intents": ["calculation"],
            "mode": "all",
        },
        "expected_retrieval": None,
        "expected_tools": [
            {"tool_name": "calculator", "params": {"expression": "(123+456)*78/3"}},
        ],
        "expected_answer": {
            "key_phrases": ["15054"],
        },
        "gold_answer": "计算结果为 15054。",
        "difficulty": "easy",
        "category": "math",
        "tags": ["math", "tool_use"],
    },
    {
        "query": "比较一下 iPhone 15 Pro 和 华为 Mate 60 Pro，推荐一个给我",
        "context": {"user_budget": "8000", "preference": "拍照"},
        "expected_intent": {
            "intents": ["product_comparison", "recommendation"],
            "mode": "at_least_n",
            "n": 2,
        },
        "expected_retrieval": {
            "doc_ids": [
                "iphone15pro_review_2024",
                "mate60pro_review_2024",
                "phone_camera_comparison_2024",
            ],
            "min_precision": 0.7,
        },
        "expected_tools": [
            {"tool_name": "search_products", "params": {"keywords": ["iPhone 15 Pro", "华为 Mate 60 Pro"]}},
            {"tool_name": "compare_specs", "params": {"product_ids": ["iphone15pro", "mate60pro"]}},
        ],
        "expected_answer": {
            "key_phrases": ["拍照", "芯片", "价格", "推荐"],
        },
        "gold_answer": "综合考虑拍照和性价比，推荐华为 Mate 60 Pro，拍照能力更强且价格在预算内。",
        "difficulty": "medium",
        "category": "ecommerce",
        "tags": ["comparison", "multi_tool"],
    },
    {
        "query": "推荐三本关于机器学习的入门书籍",
        "context": {"user_level": "beginner", "language": "中文"},
        "expected_intent": {
            "intents": ["book_recommendation"],
            "mode": "all",
        },
        "expected_retrieval": {
            "doc_ids": [
                "ml_book_intro_1",
                "ml_book_intro_2",
                "ml_book_intro_3",
            ],
            "min_precision": 0.7,
            "min_count": 3,
        },
        "expected_tools": None,
        "expected_answer": {
            "key_phrases": ["机器学习", "入门", "书籍", "推荐"],
        },
        "gold_answer": "推荐以下三本：《机器学习》（周志华）、《统计学习方法》（李航）、《深度学习入门》（斋藤康毅）。",
        "difficulty": "easy",
        "category": "education",
        "tags": ["recommendation", "retrieval"],
    },
    {
        "query": "帮我订一张明天从上海到深圳的机票",
        "context": {"user_id": "u_12345", "preferred_airline": "南航"},
        "expected_intent": {
            "intents": ["flight_booking"],
            "mode": "all",
        },
        "expected_retrieval": None,
        "expected_tools": [
            {"tool_name": "search_flights", "params": {"from": "上海", "to": "深圳", "date": "tomorrow"}},
            {"tool_name": "book_flight", "params": {"airline": "南航"}},
        ],
        "expected_answer": {
            "key_phrases": ["航班", "价格", "时间", "确认"],
        },
        "gold_answer": "已为您预订明天南航 CZ3001 航班，上海浦东 14:00 出发，深圳宝安 16:30 到达，票价 ¥1280。",
        "difficulty": "medium",
        "category": "travel",
        "tags": ["booking", "multi_tool", "action"],
    },
]


async def seed():
    """插入 seed 数据。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        # 检查是否已有数据
        from sqlalchemy import select, func
        result = await session.execute(select(func.count()).select_from(EvalCase))
        if result.scalar() > 0:
            print(f"数据库已有 {result.scalar()} 条用例，跳过 seed。")
            return

        # 创建 case_set
        case_set = CaseSet(
            name="smoke-test-set",
            description="Smoke 测试用例集，覆盖 5 种典型场景",
            category="smoke",
            version="1.0.0",
            tags=["smoke", "mvp"],
        )
        session.add(case_set)
        await session.flush()

        # 创建 eval_cases
        for i, case_data in enumerate(SEED_CASES):
            case = EvalCase(
                query=case_data["query"],
                context=case_data.get("context", {}),
                expected_intent=case_data.get("expected_intent"),
                expected_retrieval=case_data.get("expected_retrieval"),
                expected_tools=case_data.get("expected_tools", []),
                expected_answer=case_data.get("expected_answer"),
                gold_answer=case_data.get("gold_answer"),
                source="manual",
                difficulty=case_data.get("difficulty", "medium"),
                category=case_data.get("category"),
                tags=case_data.get("tags", []),
                priority=i + 1,
                review_status="approved",
            )
            session.add(case)
            await session.flush()

            # 关联到 case_set
            member = CaseSetMember(
                case_set_id=case_set.id,
                case_id=case.id,
            )
            session.add(member)

        case_set.case_count = len(SEED_CASES)
        await session.commit()

        print(f"Seed 完成：{len(SEED_CASES)} 条用例，1 个 case_set (smoke-test-set)")


if __name__ == "__main__":
    asyncio.run(seed())
