# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Service Mail — envoi d'e-mails (SMTP) pour le compte des outils.

Jumeau « consommé par les plugins » du provider Ember+ : au lieu d'interroger les
outils, ce service leur OFFRE une capacité. Un outil (ou le code serveur) lui passe
les infos du message ; le service se charge de l'expédition SMTP.

Deux chemins d'accès :
  · in-process  → helper `ctx.send_mail(subject, body, to=...)` (cf. app/routes._build_ctx
    et app/tools._system_ctx). Le helper résout ce module via core_plugins et appelle
    `enqueue(...)`.
  · HTTP        → `POST /api/mail/send` (session OU header X-BT-Mail-Token), pour les
    outils Docker qui ne partagent pas le process Python.

Envoi ASYNCHRONE best-effort : `enqueue()` dépose dans une file et rend la main aussitôt
(une alerte ratée ne doit jamais casser le flux appelant) ; un worker daemon dépile et
envoie, puis journalise le résultat dans l'audit (« sent » / « fail »). Le bouton « test »
de l'UI utilise `send_now()`, synchrone, pour un retour pass/échec immédiat.
"""
import logging
import queue
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import parseaddr

from app import settings
from app.database import audit_log

log = logging.getLogger(__name__)

# Acteur virtuel par défaut attribué dans l'audit.
MAIL_ACTOR = "Service Mail"

# ── File d'attente + worker ──────────────────────────────────
_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()
_STOP = object()                       # sentinelle d'arrêt (poison pill)

# Dernier résultat d'envoi (pour le statut UI). Protégé par _state_lock.
_state_lock = threading.Lock()
_last = {"ok": None, "detail": "", "at": None}


def _split_addrs(raw):
    """Découpe une liste d'adresses (CSV ou liste) en adresses non vides."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        parts = raw
    else:
        parts = str(raw).replace(";", ",").split(",")
    return [a.strip() for a in parts if a and a.strip()]


def _set_last(ok, detail):
    from datetime import datetime
    with _state_lock:
        _last["ok"] = ok
        _last["detail"] = detail
        _last["at"] = datetime.now().isoformat(timespec="seconds")


def _build_message(subject, body, recipients, *, html=None, cc=None, reply_to=None):
    """Construit l'EmailMessage. Lève ValueError si l'expéditeur ou les destinataires
    sont absents/invalides."""
    from_addr = (settings.get("mail_from") or "").strip()
    if not from_addr or "@" not in parseaddr(from_addr)[1]:
        raise ValueError("expéditeur (mail_from) absent ou invalide")
    cc = _split_addrs(cc)
    if not recipients and not cc:
        raise ValueError("aucun destinataire")

    msg = EmailMessage()
    msg["From"] = from_addr
    if recipients:
        msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject or ""
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _smtp_send(msg):
    """Connexion SMTP selon les réglages et envoi (synchrone, bloquant). Lève en cas d'échec."""
    host = (settings.get("mail_smtp_host") or "").strip()
    if not host:
        raise ValueError("serveur SMTP (mail_smtp_host) non configuré")
    port = int(settings.get("mail_smtp_port") or 587)
    security = (settings.get("mail_security") or "starttls").lower()
    username = settings.get("mail_username") or ""
    password = settings.get("mail_password") or ""
    timeout = int(settings.get("mail_timeout") or 15)

    # Contexte TLS VÉRIFIANT (chaîne de certification + nom d'hôte). Sans ce contexte
    # explicite, smtplib retombe sur `ssl._create_stdlib_context()` : verify_mode=CERT_NONE
    # et check_hostname=False — autrement dit le canal était chiffré mais avec N'IMPORTE
    # QUEL certificat, sans lien avec `mail_smtp_host`. Un intercepteur actif (ARP/DNS)
    # se présentait donc comme le relais et récoltait `mail_password` au `smtp.login()`
    # qui suit, sans que rien ne l'indique. Le chiffrement sans vérification d'identité
    # ne protège de rien d'autre que de l'écoute passive.
    # Conséquence assumée : un relais interne à certificat auto-signé (ou joint par IP
    # sans SAN iPAddress) est désormais REFUSÉ à la connexion, avec une erreur SSL
    # explicite remontée en audit « fail » / dans le retour du bouton test. C'est
    # volontaire : le laisser passer silencieusement était le défaut d'origine. Le jour
    # où un tel relais doit être admis, en faire un réglage explicite et visible, jamais
    # le comportement par défaut.
    ctx = ssl.create_default_context()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
    try:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls(context=ctx)
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def _deliver(item):
    """Envoie un message de la file et journalise le résultat. Ne lève pas."""
    subject, body, recipients, kw, actor = item
    try:
        msg = _build_message(subject, body, recipients, **kw)
        _smtp_send(msg)
        detail = f"to={', '.join(recipients) or '—'} · {subject or ''}"[:500]
        _set_last(True, detail)
        audit_log("mail", "sent", detail, user_id=None, username=actor)
    except Exception as e:
        detail = f"to={', '.join(recipients) or '—'} · {subject or ''} · échec : {e}"[:500]
        _set_last(False, detail)
        audit_log("mail", "fail", detail, user_id=None, username=actor)
        log.warning("mail: envoi échoué : %s", e)


def _worker_loop():
    while True:
        item = _queue.get()
        try:
            if item is _STOP:
                return
            _deliver(item)
        finally:
            _queue.task_done()


def _ensure_worker():
    """Démarre le worker si nécessaire (idempotent, thread-safe)."""
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="mail-worker", daemon=True)
        _worker.start()


# ── API publique du service ──────────────────────────────────

def enqueue(subject, body, to=None, *, html=None, cc=None, reply_to=None, actor=MAIL_ACTOR):
    """Met un message en file (asynchrone, best-effort). Retourne {"queued": bool, ...}.
    Ne lève jamais : une alerte ratée ne doit pas casser le flux appelant."""
    try:
        if not settings.get("mail_enabled"):
            return {"queued": False, "error": "service mail désactivé"}
        recipients = _split_addrs(to) or _split_addrs(settings.get("mail_default_to"))
        if not recipients and not _split_addrs(cc):
            return {"queued": False, "error": "aucun destinataire (ni 'to' ni défaut)"}
        _ensure_worker()
        _queue.put((subject, body, recipients,
                    {"html": html, "cc": cc, "reply_to": reply_to}, actor or MAIL_ACTOR))
        return {"queued": True, "to": recipients}
    except Exception as e:
        log.warning("mail: enqueue échoué : %s", e)
        return {"queued": False, "error": str(e)}


def send_now(subject, body, to=None, *, html=None, cc=None, reply_to=None, actor=MAIL_ACTOR):
    """Envoi SYNCHRONE (pour le bouton « test » de l'UI). Retourne {"ok": bool, "error"?}.
    Contourne `mail_enabled` (un test doit pouvoir valider la config avant activation)."""
    recipients = _split_addrs(to) or _split_addrs(settings.get("mail_default_to"))
    try:
        msg = _build_message(subject, body, recipients,
                             html=html, cc=cc, reply_to=reply_to)
        _smtp_send(msg)
        detail = f"to={', '.join(recipients) or '—'} · test"
        _set_last(True, detail)
        audit_log("mail", "test", detail, user_id=None, username=actor)
        return {"ok": True, "to": recipients}
    except Exception as e:
        detail = f"to={', '.join(recipients) or '—'} · test · échec : {e}"[:500]
        _set_last(False, detail)
        audit_log("mail", "test_fail", detail, user_id=None, username=actor)
        return {"ok": False, "error": str(e)}


def status_dict():
    """État du service pour l'UI ; secrets exposés en booléens *_set seulement."""
    with _state_lock:
        last = dict(_last)
    return {
        "enabled": bool(settings.get("mail_enabled")),
        "host": settings.get("mail_smtp_host") or "",
        "port": int(settings.get("mail_smtp_port") or 587),
        "security": settings.get("mail_security") or "starttls",
        "username": settings.get("mail_username") or "",
        "from": settings.get("mail_from") or "",
        "default_to": settings.get("mail_default_to") or "",
        "timeout": int(settings.get("mail_timeout") or 15),
        "password_set": bool(settings.get("mail_password")),
        "token_set": bool(settings.get("mail_token")),
        "queue_depth": _queue.qsize(),
        "last": last,
    }


def boot():
    """Démarrage au lancement de l'app (après init_db). Lance le worker si activé."""
    try:
        if settings.get("mail_enabled"):
            _ensure_worker()
    except Exception as e:
        log.error("mail: boot échoué : %s", e)


def stop():
    """Arrêt propre du worker (poison pill)."""
    global _worker
    with _worker_lock:
        w = _worker
        _worker = None
    if w and w.is_alive():
        _queue.put(_STOP)
        w.join(timeout=2)


# ═════════════════════════════════════════════════════════════════════
# Routes API du service (montées par core_plugins.register_all_routes)
# ═════════════════════════════════════════════════════════════════════

def register_routes(bp):
    """Expose les routes propres au service. Le service POSSÈDE ses routes."""
    from flask import request, jsonify
    from app.auth import require_login, require_perm, current_user

    _MASK = "••••••••"

    @bp.route("/api/mail/status", methods=["GET"])
    @require_login
    def mail_status():
        return jsonify(status_dict())

    @bp.route("/api/mail/apply", methods=["POST"])
    @require_perm("settings.edit")
    def mail_apply():
        data = request.json or {}
        enabled = bool(data.get("enabled"))
        try:
            port = int(data.get("port") or 587)
            timeout = int(data.get("timeout") or 15)
        except (TypeError, ValueError):
            return jsonify({"error": "port/délai invalide"}), 400
        if not (1 <= port <= 65535):
            return jsonify({"error": "port invalide"}), 400
        security = (data.get("security") or "starttls").lower()
        if security not in ("none", "starttls", "ssl"):
            return jsonify({"error": "sécurité invalide"}), 400

        settings.set("mail_enabled", enabled)
        settings.set("mail_smtp_host", (data.get("host") or "").strip())
        settings.set("mail_smtp_port", port)
        settings.set("mail_security", security)
        settings.set("mail_username", data.get("username") or "")
        settings.set("mail_from", (data.get("from") or "").strip())
        settings.set("mail_default_to", (data.get("default_to") or "").strip())
        settings.set("mail_timeout", timeout)
        # Secrets : mis à jour seulement si fournis et différents du masque.
        pwd = data.get("password")
        if pwd is not None and pwd != _MASK:
            settings.set("mail_password", pwd)
        tok = data.get("token")
        if tok is not None and tok != _MASK:
            settings.set("mail_token", tok)

        if enabled:
            _ensure_worker()
        u = current_user() or {}
        audit_log("mail", "apply",
                  f"enabled={enabled} host={(data.get('host') or '').strip()} "
                  f"port={port} security={security}",
                  user_id=u.get("id"), username=u.get("username") or "système")
        return jsonify(status_dict())

    @bp.route("/api/mail/test", methods=["POST"])
    @require_perm("settings.edit")
    def mail_test():
        data = request.json or {}
        u = current_user() or {}
        res = send_now(data.get("subject") or "Bobi.Tools — test",
                       data.get("body") or "Ceci est un e-mail de test depuis Bobi.Tools.",
                       to=data.get("to"),
                       actor=u.get("username") or MAIL_ACTOR)
        return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 502)

    @bp.route("/api/mail/send", methods=["POST"])
    def mail_send():
        # Auth : session connectée OU token partagé (outils Docker).
        token = settings.get("mail_token") or ""
        header = request.headers.get("X-BT-Mail-Token") or ""
        authed = bool(current_user())
        if not authed:
            if not token or header != token:
                return jsonify({"error": "non autorisé"}), 401
        data = request.json or {}
        u = current_user() or {}
        actor = (u.get("username") if authed else None) or "outil (token)"
        res = enqueue(data.get("subject") or "", data.get("body") or "",
                      to=data.get("to"), html=data.get("html"),
                      cc=data.get("cc"), reply_to=data.get("reply_to"), actor=actor)
        return (jsonify(res), 200) if res.get("queued") else (jsonify(res), 400)
