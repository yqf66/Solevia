"""Narrow adapter for the installed Crawl4AI BrowserManager API.

The owning Node service supplies a DNS-pinned public-only proxy. The adapter
enables Chromium's OS sandbox and configures contexts before any page exists.
"""
from planner_api.worker_sandbox import worker_environment


def contain_crawler(crawler):
    manager = crawler.crawler_strategy.browser_manager
    build = manager._build_browser_args

    def sandboxed_args():
        options = build()
        unsafe = {"--no-sandbox", "--disable-setuid-sandbox", "--ignore-certificate-errors",
                  "--ignore-certificate-errors-spki-list", "--disable-ipc-flooding-protection"}
        options["args"] = [arg for arg in options["args"] if arg not in unsafe]
        options["args"] += ["--proxy-bypass-list=<-loopback>",
                            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
        options["chromium_sandbox"] = True
        options["env"] = worker_environment()
        return options

    manager._build_browser_args = sandboxed_args
    create = manager.create_browser_context

    async def sandboxed_context(*args, **kwargs):
        browser = manager.browser
        if not getattr(browser, "_plan_a_sandboxed", False):
            new_context = browser.new_context

            async def guarded_context(**settings):
                settings.update(service_workers="block", accept_downloads=False,
                                ignore_https_errors=False)
                context = await new_context(**settings)
                async def close_websocket(socket):
                    await socket.close()
                await context.route_web_socket("**/*", close_websocket)
                return context

            browser.new_context = guarded_context
            browser._plan_a_sandboxed = True
        return await create(*args, **kwargs)

    manager.create_browser_context = sandboxed_context
