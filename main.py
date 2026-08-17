"""入口：初始化数据库，同时启动 bot 轮询与 Web 服务。"""
import asyncio
import logging

import uvicorn

import config
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def run_web():
    server = uvicorn.Server(uvicorn.Config(
        "web:app", host=config.WEB_HOST, port=config.WEB_PORT, log_level="info"))
    await server.serve()


async def main():
    await db.init()
    if not config.BOT_TOKEN:
        print("警告：未设置 BOT_TOKEN，机器人不会启动。请复制 .env.example 为 .env 并填 token。")

    from bot import run_bot
    tasks = [run_web()]
    if config.BOT_TOKEN:
        tasks.append(run_bot(config.BOT_TOKEN))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
