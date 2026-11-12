"""Minimal first-party HTML for the two user-initiated email actions.

The GET page never consumes a token. A user click triggers the authenticated
single-use POST API. No third-party scripts, cookies, or external assets.
"""
import json

from fastapi.responses import HTMLResponse


_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def render_email_action_page(token: str, purpose: str) -> HTMLResponse:
    if purpose not in {"verify", "reset"}:
        raise ValueError("invalid email action")
    safe_token = json.dumps(token, ensure_ascii=True).replace("<", "\\u003c")
    verify = purpose == "verify"
    title = "Подтверждение email" if verify else "Восстановление пароля"
    button = "Подтвердить email" if verify else "Изменить пароль"
    endpoint = "/api/v1/auth/email/verify" if verify else "/api/v1/auth/password-reset/confirm"
    password_input = (
        '' if verify else
        '<label for="password">Новый пароль (10–128 символов)</label>'
        '<input id="password" type="password" autocomplete="new-password" '
        'minlength="10" maxlength="128" required>'
    )
    body = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<title>__TITLE__ — ByMeVPN</title>
<style>
:root{color-scheme:dark}body{min-height:100vh;display:grid;place-items:center;
margin:0;background:#07111d;color:#f3f7fb;font:16px/1.5 system-ui,sans-serif}
main{width:min(100% - 36px,440px)}h1{font-size:26px;margin:0 0 12px}
p{color:#a9b9c8}label{display:block;font-weight:600;margin-top:24px}
input{box-sizing:border-box;width:100%;background:#152536;color:#fff;border:1px solid
#41576d;padding:14px;border-radius:10px;font:inherit;margin:8px 0 16px}
button{width:100%;background:#158fff;color:#fff;border:0;border-radius:10px;
padding:15px;font:600 16px system-ui;cursor:pointer}button:disabled{opacity:.6}
#status{margin-top:18px}a{color:#8ad5ff}
</style></head><body><main><h1>__TITLE__</h1>
<p>Ссылка одноразовая и действует ограниченное время. Никому её не передавайте.</p>
<form id="form">__INPUT__<button id="confirm" type="submit">__BUTTON__</button></form>
<p id="status" role="status" aria-live="polite"></p>
<p><a href="https://t.me/ByMeVPNSupportBot">Поддержка ByMeVPN</a></p>
</main><script>
const ticket=__TOKEN__,purpose=__PURPOSE__,endpoint=__ENDPOINT__;
const form=document.getElementById('form'),status=document.getElementById('status');
form.addEventListener('submit',async(e)=>{
 e.preventDefault();const button=document.getElementById('confirm');button.disabled=true;
 status.textContent='Проверяем ссылку…';
 const body={token:ticket};
 if(purpose==='reset')body.password=document.getElementById('password').value;
 try{
  const response=await fetch(endpoint,{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify(body),
   credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer'});
  if(!response.ok)throw new Error('expired');
  form.hidden=true;
  status.textContent=purpose==='reset'
    ?'Пароль изменён. Все прежние сессии завершены. Войдите в приложение заново.'
    :'Email подтверждён. Вернитесь в приложение и войдите в свой аккаунт.';
 }catch(_){
  status.textContent='Не удалось подтвердить действие. Запросите новую ссылку в приложении.';
  button.disabled=false;
 }
});
</script></body></html>"""
    body = (body.replace("__TITLE__", title).replace("__INPUT__", password_input)
            .replace("__BUTTON__", button).replace("__TOKEN__", safe_token)
            .replace("__PURPOSE__", json.dumps(purpose))
            .replace("__ENDPOINT__", json.dumps(endpoint)))
    return HTMLResponse(body, headers={
        "Cache-Control": "no-store, max-age=0", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex,nofollow", "Content-Security-Policy": _CSP,
    })
