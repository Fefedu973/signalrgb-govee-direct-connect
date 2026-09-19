# Validation de la reprise UDP Windows — 19 septembre 2026

Le correctif du listener est validé sans action sur les ampoules : **29 tests passent**, dont 25 tests existants et quatre tests Windows supplémentaires. Exécution sous Python 3.12.14, avec sa boucle `asyncio.ProactorEventLoop` réelle.

```powershell
python -B -m unittest discover -p "test_*.py" -v
```

| Test | Résultat observé |
| --- | --- |
| Fermeture de clients réels | 20 cycles sur ports éphémères loopback : requête, fermeture du client, ACK tardif, puis aller-retour réussi avec un nouveau client. |
| Récepteur témoin | Une faute `OSError(10054)` injectée lors du réarmement de `Proactor.recvfrom` laisse le transport ouvert, mais la requête suivante n'est plus reçue. |
| Récepteur corrigé | La même faute traverse le véritable callback Proactor ; le listener se ferme et se recrée. La requête suivante reçoit une réponse, avec une seule instance Bridge, un seul démarrage et aucune fermeture du Bridge pendant la reprise. |
| Échec borné | Trois échecs de réouverture injectés entraînent la fermeture propre du Bridge et la libération du port. Aucun quatrième essai n'est effectué. |

Les sessions BLE des tests sont remplacées par un double : ces résultats vérifient la conservation de l'objet de session et l'ordre du nettoyage, pas une reconnexion radio réelle. Aucun paquet n'est envoyé au LAN, à une ampoule ou à un service existant.

## Cause constatée et hypothèse

Sur le processus réel examiné par l'agent principal, le port UDP restait lié mais trois demandes de statut échouaient. Le processus attendait dans la boucle asyncio. Les dernières écritures BLE précédaient immédiatement un redémarrage de SignalRGB. Ce constat est compatible avec l'arrêt du réarmement de réception présent dans le code CPython 3.12.14.

L'hypothèse d'un ICMP `PORT_UNREACHABLE` devenu erreur Windows 10054 reste **non prouvée sur cet incident** : les essais avec fermeture de clients n'ont pas émis cette erreur, même avec le signalement explicitement activé. Le test injecté prouve le comportement du chemin d'erreur et sa reprise, sans prétendre reproduire l'origine réseau du défaut réel.

Le correctif ajoute deux protections complémentaires : suppression explicite des notifications `SIO_UDP_CONNRESET` sur chaque nouveau socket Windows, puis journalisation et remplacement du listener pour toute erreur UDP signalée. Les logs ne contiennent ni paquets, ni jetons, ni clés. Les réouvertures restent limitées à trois par période de 30 secondes.

Source primaire : [Microsoft — Winsock IOCTLs, SIO_UDP_CONNRESET](https://learn.microsoft.com/en-us/windows/win32/winsock/winsock-ioctls).

Cette validation ne remplace pas la vérification en fonctionnement après installation, effectuée séparément par l'agent principal.
