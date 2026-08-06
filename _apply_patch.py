with open(r"E:\codex\eh-bot\main.py", encoding="utf-8") as f:
    content = f.read()

# Remove proxy pool refresh from main
old = """    # 初始化代理池
    await pool.refresh(force=True)

    async with httpx.AsyncClient("""
new = """    async with httpx.AsyncClient("""
content = content.replace(old, new)

# Remove proxy_pool import
content = content.replace("from proxy_pool import pool, PROXY_SOURCES\n", "")

with open(r"E:\codex\eh-bot\main.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(content)
print("removed proxy pool")