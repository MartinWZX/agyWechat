#!/usr/bin/env python3
"""WeChat (Weixin) Personal Account QR Code Login Tool for WeChat-AGY Bridge.

This tool communicates with Tencent's official iLink Bot API to generate a login QR code,
waits for mobile WeChat confirmation, and automatically saves the credentials to .env.
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

# Auto re-exec into project virtualenv if needed
PROJECT_ROOT = Path(__file__).resolve().parent
venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
if venv_python.exists() and sys.executable != str(venv_python):
    os.execl(str(venv_python), str(venv_python), *sys.argv)

sys.path.insert(0, str(PROJECT_ROOT))

from app.ilink_gateway import ILinkAPIClient, ILINK_DEFAULT_BASE_URL


def print_ascii_qr(url_or_code: str, qr_img_path: Path) -> None:
    """Render ASCII QR code in terminal and save PNG image."""
    print(f"\n[QR_URL]: {url_or_code}\n")
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(url_or_code)
        qr.make(fit=True)
        print("\n" + "=" * 46)
        print("   请使用手机微信扫描以下二维码登录   ")
        print("=" * 46 + "\n")
        qr.print_ascii(invert=True)
        print("\n" + "=" * 46)

        img = qr.make_image(fill_color="black", back_color="white")
        img.save(str(qr_img_path))
        print(f"🖼️ 二维码图片已保存至: {qr_img_path}")

        # Optional: copy to artifact directory if AGY_BRAIN_DIR is set
        brain_env = os.environ.get("AGY_BRAIN_DIR")
        if brain_env:
            brain_dir = Path(brain_env)
            if brain_dir.exists():
                try:
                    shutil.copyfile(str(qr_img_path), str(brain_dir / "wechat_qr.png"))
                except Exception:
                    print(f"⚠️ 无法同步二维码至 brain 目录: {brain_dir}")

        # Optional: auto-detect WSL Windows desktop for visual convenience
        users_dir = Path("/mnt/c/Users")
        if users_dir.exists():
            for u in users_dir.iterdir():
                if u.is_dir() and u.name not in ("Default", "Public", "All Users", "defaultuser0"):
                    desktop = u / "Desktop"
                    if desktop.is_dir():
                        try:
                            shutil.copyfile(str(qr_img_path), str(desktop / "wechat_qr.png"))
                            print(f"🖥️ 二维码已同步放置在桌面: {desktop / 'wechat_qr.png'}")
                            break
                        except Exception:
                            print(f"⚠️ 桌面同步失败，保留本地终端二维码")
    except Exception as e:
        print(f"⚠️ 终端渲染二维码提示: {e}")
        print(f"🌐 请直接在浏览器中打开二维码链接:\n   {url_or_code}\n")


def update_env_file(env_path: Path, new_vars: dict) -> None:
    """Safely update or append key-value pairs in the .env file."""
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in new_vars:
                new_lines.append(f"{k}={new_vars[k]}")
                updated_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in new_vars.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


async def async_login(timeout_seconds: int = 600) -> bool:
    """Execute the full QR code login and polling flow."""
    print("==================================================")
    print("🤖 WeChat-AGY Bridge 微信个人号扫码直连")
    print("==================================================")
    print("正在请求微信登录二维码，请稍候...\n")

    try:
        qrcode_val, qrcode_url = await ILinkAPIClient.fetch_qr_code(bot_type="3")
    except Exception as e:
        print(f"❌ 获取二维码失败: {e}")
        return False

    if not qrcode_val:
        print("❌ 微信服务器未返回有效二维码信息。")
        return False

    qr_img_path = PROJECT_ROOT / "wechat_qr.png"
    print_ascii_qr(qrcode_url or qrcode_val, qr_img_path)

    print("\n等待扫码确认中（支持手机微信后置摄像头扫码）...")
    deadline = time.monotonic() + timeout_seconds
    refresh_count = 0
    current_base_url = ILINK_DEFAULT_BASE_URL

    while time.monotonic() < deadline:
        try:
            status_resp = await ILinkAPIClient.poll_qr_status(
                qrcode=qrcode_val,
                base_url=current_base_url,
                timeout_seconds=35,
            )
        except Exception as e:
            await asyncio.sleep(1)
            continue

        status = str(status_resp.get("status") or "wait")

        if status == "wait":
            print(".", end="", flush=True)
        elif status == "scaned":
            print("\n📲 微信已扫码！请在手机端点击【确认登录】...")
        elif status == "scaned_but_redirect" and status_resp.get("redirect_host"):
            current_base_url = f"https://{status_resp['redirect_host']}"
        elif status == "expired":
            refresh_count += 1
            if refresh_count > 8:
                print("\n❌ 二维码已超时多次失效，请重新运行本程序。")
                return False
            print(f"\n🔄 二维码已过期，正在自动刷新 ({refresh_count}/8)...")
            try:
                qrcode_val, qrcode_url = await ILinkAPIClient.fetch_qr_code(base_url=current_base_url, bot_type="3")
                print_ascii_qr(qrcode_url or qrcode_val, qr_img_path)
            except Exception as e:
                print(f"❌ 刷新二维码失败: {e}")
                return False
        elif status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            bot_token = str(status_resp.get("bot_token") or "")
            user_id = str(status_resp.get("ilink_user_id") or "")
            base_url = str(status_resp.get("baseurl") or current_base_url)

            if not account_id or not bot_token:
                print("\n❌ 微信授权已确认，但返回凭证不完整。")
                return False

            env_path = PROJECT_ROOT / ".env"
            allowed_users_val = f'["{user_id}"]' if user_id else '["*"]'
            update_env_file(env_path, {
                "WECHAT_CHANNEL": "ilink",
                "WEIXIN_ACCOUNT_ID": account_id,
                "WEIXIN_TOKEN": bot_token,
                "WEIXIN_BASE_URL": base_url,
                "ALLOWED_USERS": allowed_users_val,
            })

            print("\n" + "=" * 50)
            print("🎉 恭喜！微信端已成功连接绑定！")
            print(f"• 专属机器人 ID: {account_id}")
            if user_id:
                print(f"• 绑定微信用户: {user_id}")
            print(f"• 配置已自动写入: {env_path}")
            print("=" * 50)
            print("\n🚀 现在您只需启动服务，即可直接在微信中发送指令与 AGY 交互：")
            print("   ./run.sh")
            print("=" * 50)
            return True

    print("\n⌛ 扫码超时，请重新运行。")
    return False


def main():
    try:
        success = asyncio.run(async_login())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n已取消登录。")
        sys.exit(0)


if __name__ == "__main__":
    main()
