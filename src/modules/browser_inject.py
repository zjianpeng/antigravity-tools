"""生成网页端 Bearer 注入脚本（右键"复制网页注入脚本"使用）。

原理（已实测验证，2026-08-03）：
- 网页端 console API 走 APISIX 网关，同时接受 session cookie 和
  `Authorization: Bearer <accessToken>` 头（Bearer 与 cookie 并存时 Bearer 生效）；
  但网页 JS 自身只带 cookie，从不设置 Authorization。
- 脚本劫持页面的 XMLHttpRequest / fetch，给同源请求自动补 Bearer 头。
  仅存在于页面运行时，刷新即失效，不落盘、不改 cookie、无需手机验证码。

用法：在 workbuddy.cn / codebuddy.cn 页面按 F12 → 控制台（Console）粘贴回车。
"""

import json


def build_inject_js(access_token: str) -> str:
    """生成注入脚本（仅同源请求注入 Bearer，附带验证日志与标志位）。"""
    token_json = json.dumps(access_token)  # 安全转义为 JS 字符串字面量
    return f"""(() => {{
  const TOKEN = {token_json};
  const sameHost = (u) => {{
    try {{ return new URL(u, location.href).origin === location.origin; }} catch {{ return false; }}
  }};
  const _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (...args) {{
    const r = _open.apply(this, args);
    try {{ if (sameHost(args[1])) this.setRequestHeader("Authorization", "Bearer " + TOKEN); }} catch (e) {{}}
    return r;
  }};
  const _fetch = window.fetch;
  window.fetch = function (url, opts = {{}}) {{
    const u = typeof url === "string" ? url : url.url;
    if (sameHost(u)) {{
      const headers = new Headers(opts.headers || {{}});
      headers.set("Authorization", "Bearer " + TOKEN);
      return _fetch.call(this, url, {{ ...opts, headers }});
    }}
    return _fetch.call(this, url, opts);
  }};
  fetch('/console/accounts', {{credentials:'include'}}).then(r=>r.json()).then(d=>{{
    const a=(d.data&&d.data.accounts&&d.data.accounts[0])||{{}};
    console.log('[antigravity-tools] 注入完成，当前网页账号:', a.nickname||'', a.phoneNumber||a.uid||'');
  }}).catch(e=>console.warn('[antigravity-tools] 验证请求失败', e));
  window.__AGT_INJECTED__ = true;
  return 'AGT_INJECT_OK';
}})();"""
