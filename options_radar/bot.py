from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from typing import Optional

from .parser import contract_key
from .pipeline import RadarPipeline
from .reports import daily_report, portfolio_markdown, write_daily_report
from .timeutil import us_session_date_from_china_time


def _chunks(text: str, limit: int = 1900):
    while text:
        if len(text) <= limit:
            yield text
            break
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 200 else limit
        yield text[:cut]
        text = text[cut:].lstrip()


def run_bot(pipeline: RadarPipeline) -> None:
    try:
        import discord  # type: ignore
        from discord import app_commands  # type: ignore
        from discord.ext import tasks  # type: ignore
    except ImportError as exc:
        raise RuntimeError("discord.py is not installed; install requirements.txt") from exc

    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is empty")
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    discord_settings = pipeline.config.section("discord")
    guild_id = int(discord_settings.get("command_guild_id", 0) or 0)
    destination_id = int(discord_settings.get("destination_channel_id", 0) or 0)
    daily_time = str(discord_settings.get("daily_report_local_time", "05:30"))
    last_daily_report = {"date": None}

    async def in_worker(function, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: function(*args))

    @client.event
    async def on_ready():
        if guild_id:
            guild = discord.Object(id=guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        if not daily_report_loop.is_running():
            daily_report_loop.start()

    @tasks.loop(minutes=1)
    async def daily_report_loop():
        if not destination_id:
            return
        now = datetime.now()
        if now.strftime("%H:%M") != daily_time or last_daily_report["date"] == now.date():
            return
        channel = client.get_channel(destination_id)
        if channel:
            session_date = us_session_date_from_china_time(now)
            write_daily_report(pipeline.database, session_date, pipeline.config.root / "reports")
            for chunk in _chunks(daily_report(pipeline.database, session_date)):
                await channel.send(chunk)
            last_daily_report["date"] = now.date()

    @tree.command(name="collect_today", description="采集今天的异常期权及四个分析师频道")
    async def collect_today(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        results = await in_worker(pipeline.collect_today, us_session_date_from_china_time(datetime.now()))
        rendered = pipeline.render_results(results) or "今天尚未提取到可解析的分析师卡片。"
        for index, chunk in enumerate(_chunks(rendered)):
            if index == 0:
                await interaction.followup.send(chunk, ephemeral=True)
            else:
                await interaction.followup.send(chunk, ephemeral=True)
        if destination_id:
            channel = client.get_channel(destination_id)
            if channel:
                for result in results:
                    if result.evaluation.grade == "A":
                        for chunk in _chunks(pipeline.render_results([result])):
                            await channel.send(chunk)

    @tree.command(name="report", description="发布指定日期的异常期权日报")
    @app_commands.describe(day="YYYY-MM-DD，留空为今天")
    async def report(interaction: discord.Interaction, day: Optional[str] = None):
        report_date = date.fromisoformat(day) if day else us_session_date_from_china_time(datetime.now())
        text = daily_report(pipeline.database, report_date)
        write_daily_report(pipeline.database, report_date, pipeline.config.root / "reports")
        await interaction.response.send_message(next(_chunks(text)), ephemeral=True)

    @tree.command(name="contract", description="查看一张合约最近的融合评价")
    async def contract(interaction: discord.Interaction, symbol: str, expiry: str, strike: float, option_type: str):
        key = contract_key(symbol, date.fromisoformat(expiry), strike, option_type)
        row = pipeline.database.latest_recommendation(key)
        if row is None:
            await interaction.response.send_message(f"尚无 {key} 的评价。", ephemeral=True)
            return
        payload = json.loads(str(row["payload_json"]))
        await interaction.response.send_message(
            f"**{payload['grade']} {key}** · {payload['score']}/100 · {payload['final_direction']}\n"
            + "；".join(payload.get("risk_flags", [])[:5]),
            ephemeral=True,
        )

    @tree.command(name="portfolio", description="同步并显示富途持仓与自选匹配")
    async def portfolio(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        contexts = await in_worker(pipeline.sync_portfolio)
        await interaction.followup.send(next(_chunks(portfolio_markdown(contexts))), ephemeral=True)

    @tree.command(name="analysts", description="显示分析师权重和样本数")
    async def analysts(interaction: discord.Interaction):
        rows = pipeline.database.analyst_rows()
        text = "# 分析师权重\n" + "\n".join(
            f"- {row['analyst'].upper()}: {float(row['weight']):.2f}（{row['sample_count']}个样本）" for row in rows
        )
        await interaction.response.send_message(text, ephemeral=True)

    client.run(token)
