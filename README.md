# Mail — service Bobi.Tools

Service d'envoi d'**e-mails (SMTP)** pour [Bobi.Tools](https://github.com/bob-integration/bobitools) :
les outils lui confient leurs alertes et notifications, il se charge de l'expédition.

Un service n'apparaît pas au lanceur : il se règle dans **Réglages → Système → Mail**.

## Ce que fait le service

- **Envoie par SMTP** : hôte, port (587 par défaut), sécurité STARTTLS, SSL/TLS ou aucune,
  identifiants, expéditeur et destinataires par défaut des alertes.
- **Sert les outils in-process** par `ctx.send_mail(sujet, corps, to=…)`.
- **Sert les outils en conteneur** par `POST /api/mail/send`, avec une session ou l'en-tête
  `X-BT-Mail-Token` (jeton partagé `mail_token`).
- **N'attend pas** : l'envoi passe par une file d'attente ; une alerte ratée ne bloque jamais
  l'outil qui l'émet. Le résultat est journalisé au nom de « Service Mail ».
- **Bouton « Envoyer un test »**, synchrone, pour vérifier la configuration tout de suite.

Utilisé notamment par [Tests réseau](https://github.com/bob-integration/bobitools-plugin-net_tests),
[Pilotage de switch](https://github.com/bob-integration/bobitools-plugin-switch_ports) et
[NAT multicast](https://github.com/bob-integration/bobitools-plugin-mcast_nat).

## Prérequis

- Aucun : le service tourne dans Bobi.Tools, sans dépendance Python supplémentaire.
- Un serveur SMTP joignable depuis la machine.

## Installation

Dans Bobi.Tools : **Réglages → Outils → Catalogue**, bouton « Installer ». Ou, sur une machine
neuve, en une ligne :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh) --outils mail
```

Un service se charge au démarrage : redémarrer Bobi.Tools après l'installation. Il est
désactivé par défaut : l'activer dans **Réglages → Système → Mail**.

## Sécurité

Le mot de passe SMTP et le jeton partagé sont masqués dans l'interface et retirés du journal
d'audit.

## In English

SMTP mail service for Bobi.Tools: tools hand it their alerts through `ctx.send_mail(...)`
(in-process) or `POST /api/mail/send` with a shared token (Docker tools). Sending is queued and
never blocks the caller; results are audited. Configure it under Settings → System → Mail.

## Licence

GPL-3.0-or-later — © 2026 BOBI SAS. Voir [LICENSE](LICENSE).
