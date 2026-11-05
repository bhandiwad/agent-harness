"""Offline browser automation inside the same job sandbox; no external network access."""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

request = json.loads(Path(sys.argv[1]).read_text())
html = (Path("/workspace") / request["html_path"]).read_text()
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        chromium_sandbox=False,
        args=["--disable-dev-shm-usage", "--disable-background-networking"],
    )
    context = browser.new_context(service_workers="block", accept_downloads=False)
    context.route("**/*", lambda route: route.abort())
    context.route_web_socket("**/*", lambda socket: socket.close())
    page = context.new_page()
    page.set_default_timeout(3000)
    page.set_content(html, wait_until="domcontentloaded", timeout=5000)
    observations = []
    for step in request["steps"]:
        target = page.locator(step["selector"])
        if step["action"] == "click":
            target.click()
        elif step["action"] == "fill":
            target.fill(step["value"])
        elif step["action"] == "text":
            observations.append(target.inner_text()[:4096])
    print(
        json.dumps(
            {
                "title": page.title(),
                "text": page.locator("body").inner_text()[:8192],
                "observations": observations,
            }
        )
    )
    browser.close()
