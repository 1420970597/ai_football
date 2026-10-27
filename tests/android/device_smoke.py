"""Run only inside Docker against the project-owned temporary Android emulator.

This fixture exercises native navigation and WebView cookies with simulated pages;
it does not validate production accounts, upstream data or betting.
"""
import argparse
import pathlib
import re
import subprocess
import time
import xml.etree.ElementTree as ET

parser = argparse.ArgumentParser()
parser.add_argument("phase", choices=["first_launch", "untrusted_tls", "pair", "flows", "clear_login"])
parser.add_argument("--adb", default="/source/android/build/tools/adb")
parser.add_argument("--host", default="ai-football-v2-android-device")
args = parser.parse_args()
output = pathlib.Path("/source/reports/android-device")
output.mkdir(parents=True, exist_ok=True)
base = [args.adb, "-H", args.host, "-P", "5037", "-s", "emulator-5554"]


def adb(*values, binary=False):
    result = subprocess.run(base + list(values), capture_output=True, timeout=50, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def tree():
    adb("shell", "uiautomator", "dump", "/sdcard/ballnews-window.xml")
    return ET.fromstring(adb("shell", "cat", "/sdcard/ballnews-window.xml"))


def wait_text(text, exact=False, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        nodes = list(tree().iter("node"))
        for node in nodes:
            value = node.get("text", "")
            if value == text if exact else text in value:
                return node
        time.sleep(1)
    raise AssertionError(f"UI text not found: {text}")


def tap(node):
    left, top, right, bottom = map(int, re.findall(r"\d+", node.attrib["bounds"]))
    adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))


def click(text, exact=True):
    tap(wait_text(text, exact=exact))


def input_service(url):
    node = next(n for n in tree().iter("node") if n.get("resource-id", "").endswith(":id/service_input"))
    tap(node)
    adb("shell", "input", "keyevent", "123")
    adb("shell", "input", "keyevent", *("67" for _ in range(len(node.get("text", "")) + 1)))
    adb("shell", "input", "text", url)
    # The URI field deliberately opens the input method; dismiss it before pressing the form button.
    adb("shell", "input", "keyevent", "4")


def screenshot(name):
    (output / f"{name}.png").write_bytes(adb("exec-out", "screencap", "-p", binary=True))
    (output / f"{name}.xml").write_text(ET.tostring(tree(), encoding="unicode"), encoding="utf-8")


if args.phase == "first_launch":
    adb("shell", "am", "start", "-W", "-n", "com.aifootball.platform.dev/com.aifootball.platform.MainActivity")
    wait_text("连接球讯服务")
    screenshot("01-first-launch")
    input_service("http://unsafe.invalid")
    click("验证并连接")
    wait_text("请使用有效")
    screenshot("02-http-blocked")
elif args.phase == "untrusted_tls":
    input_service("https://localhost:9443")
    click("验证并连接")
    wait_text("安全证书验证失败")
    screenshot("03-untrusted-certificate")
elif args.phase == "pair":
    input_service("https://localhost:9443")
    click("验证并连接")
    wait_text("确认连接服务")
    screenshot("04-pair-confirmation")
    click("连接")
    wait_text("比分首页（隔离样本）")
    screenshot("05-paired-webview")
elif args.phase == "flows":
    click("专家阅读")
    wait_text("登录（隔离样本）")
    click("登录样本")
    wait_text("专家推荐（隔离样本）")
    screenshot("06-session-and-expert-reading")
    click("查看样本详情")
    wait_text("推荐详情（隔离样本）")
    adb("shell", "input", "keyevent", "4")
    wait_text("专家推荐（隔离样本）")
    click("模拟会话过期")
    wait_text("登录（隔离样本）")
    screenshot("07-expired-session")
    click("设置")
    click("刷新页面")
    wait_text("比分首页（隔离样本）")
    click("外部网站")
    wait_text("打开外部链接", exact=True)
    screenshot("08-external-confirmation")
    click("取消")
    click("危险地址")
    wait_text("比分首页（隔离样本）")
    click("模拟服务故障")
    wait_text("服务返回错误（503）")
    screenshot("09-http-failure")
    click("重试")
    wait_text("比分首页（隔离样本）")
elif args.phase == "clear_login":
    click("专家阅读")
    click("登录样本")
    wait_text("专家推荐（隔离样本）")
    click("设置")
    click("清除本机登录")
    wait_text("将清除此应用内")
    click("清除本机登录")
    wait_text("比分首页（隔离样本）")
    click("专家阅读")
    wait_text("登录（隔离样本）")
    screenshot("10-cleared-login")
print(f"ANDROID_DEVICE_PHASE_OK {args.phase}")
