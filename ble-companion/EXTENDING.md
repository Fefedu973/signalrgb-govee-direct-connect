# Ajouter un modèle au pont BLE

Ce dossier contient l'extension du pont aux profils BLE explicites. Le H6159 réel en firmware1.07.02, révisions matérielles rapportées1.00.01/1.03.05, a passé une validation matérielle bornée le19septembre2026 : trois couleurs, luminosités40/60/80%, lectures d'état et restitution exacte du blanc initial, y compris son second triplet RGB D6E1FF lors de la répétition finale. L'utilisateur a confirmé : « Oui, tout fonctionne ». Voir `H6159-VALIDATION.md`. Ce résultat ne démontre pas la compatibilité de toutes les révisions H6159 et ne signifie pas à lui seul que le code est installé.

## Répartition des responsabilités

| Élément | Responsabilité |
|---|---|
| `profiles.py` | Catalogue explicite de profils, capacités, limites de cadence, constructeurs de paquets et décodeur/restauration de l'état. |
| `classic_transport.py` | Découverte autorisée, contrôle adresse/nom/GATT, notifications, requêtes et écritures de la famille classique en clair. |
| `classic_session.py` | Session sérialisée, snapshot avant modification, acquisition, couleurs, luminosité, alimentation, reprise et restitution. |
| `transport.py` + `BulbSession` | Chemin H6008 authentifié déjà établi, conservé séparément. |
| `bridge.py` | Validation de configuration, sélection de famille, API loopback, catalogue des périphériques autorisés et supervision. |
| Client SignalRGB générique | Découverte du catalogue du pont, une couleur par appareil non adressable et commandes exprimées en RGB/état, sans construire de paquets BLE. |

Un nouveau modèle utilisant exactement une famille existante peut être ajouté par un profil, une entrée de configuration et ses fixtures. **Il ne demande pas de modification du client JavaScript générique.** Cette propriété ne s'étend pas automatiquement à une nouvelle capacité de rendu : un périphérique réellement adressable ou un écran ne devient pas pris en charge en ajoutant simplement son SKU.

## Contrat du catalogue Python

```python
from profiles import get_profile, list_profiles, profile_for_device

profiles = list_profiles()                 # tuple d'objets Profile, trié par id
classic = get_profile('h6159-classic-v1')
legacy_default = profile_for_device({})   # h6008-realtime-v1, compatibilité historique
```

Chaque `Profile` expose `id`, `model`, `family`, `bluetooth_only`, `capabilities`, `minimum_interval` en secondes et `authentication`. Les capacités sont `rgb`, `brightness`, `power`, `restore` et `addressable`. Elles décrivent le chemin réellement implémenté par le pont. Ainsi, power/brightness sont faux dans les métadonnées H6008 du pont actuel, même si ces fonctions existent par ailleurs sur le matériel.

Pour la famille classique, les fonctions sont :

```text
color(rgb[3]) -> paquet bytes20
power(bool) -> paquet bytes20
brightness(pourcentage entier0..100) -> paquet bytes20
decode_snapshot(réponseAA01, réponseAA04, réponseAA05) -> dict
restore_commands(snapshot) -> liste de paquets bytes20
matches_color_mode(mode bytes17, rgb[3]) -> bool
```

Le snapshot classique contient `power` entier0/1, `brightness_raw` entier0..255, `brightness` en pourcentage d'affichage, `mode` bytes17 et `rgb` tuple. La restitution utilise la valeur brute exacte ; elle ne reconvertit jamais le pourcentage arrondi. Les constructeurs H6008 restent `None` dans ce catalogue : sa session existante possède déjà son protocole authentifié.

`write_response` est une préférence ATT facultative du profil. H6159 impose `True` : la bande testée accepte les write requests et retourne ses états ainsi, bien que sa caractéristique n'annonce que read/write-without-response. Il ne faut pas généraliser cette anomalie à un modèle inconnu. Les requêtes classiques ont deux tentatives au maximum, chacune bornée à3s ; ce retry ne s'applique pas aux commandes de couleur ou d'alimentation.

`h6159-classic-v1` impose100ms entre couleurs. Le profil H6008 autorise techniquement50ms pour conserver l'option expérimentale préexistante ; la configuration normale demeure100ms. Ne pas réduire un délai sur le seul fondement d'un acquittement logiciel : cela ne mesure pas la cadence optique.

## Ajouter un modèle compatible

1. Identifier précisément SKU, révision matérielle, firmware, nom annoncé et GATT. Conserver les sources primaires et des captures limitées aux paquets utiles. Une ressemblance commerciale ou un préfixe de nom ne prouve ni les commandes ni l'authentification.
2. Vérifier les enveloppes, domaines, longueurs, checksum, ordre RGB, plage de luminosité et signification des réponses. Distinguer une requête de lecture d'une commande de contrôle, même si leur domaine numérique est identique.
3. Ajouter dans `_PROFILES` un identifiant de version explicite, par exemple `modele-classic-v1`, avec le modèle exact et `family='classic'` seulement si son protocole, ses propriétés GATT et ses réponses sont compatibles. Réutiliser les fonctions actuelles uniquement lorsque les preuves établissent la même sémantique.
4. Fournir les paquets de référence anonymisés, puis les tests de décodage, d'entrées invalides et de restauration. Un changement de plage de luminosité, de payload ou de champs de mode nécessite des fonctions propres au profil ; éviter une détection approximative à partir de la valeur reçue.
5. Ajouter l'appareil à la configuration privée avec ce profil et son adresse explicitement autorisée. Un SKU inconnu n'est jamais activé automatiquement par le scanner. Le nom affiché est indépendant de l'identifiant stable.
6. Vérifier la configuration sans ouvrir Bluetooth, puis effectuer une seule validation matérielle coordonnée : identité, GATT, lectures d'état, changement borné, restitution et relecture de l'état. Les mocks ne remplacent pas cette dernière preuve.

Exemple fictif pour la bande H6159, à insérer dans `devices` :

```json
{
  "device": "shelf-strip",
  "profile": "h6159-classic-v1",
  "name": "bande étagère",
  "ble_address": "00:00:00:00:00:01",
  "advertised_name": "ihoment_H6159_EXAMPLE"
}
```

L'adresse et le nom annoncé de cet exemple sont fictifs. `advertised_name` est facultatif ; s'il est fourni, un nom effectivement observé doit lui correspondre exactement. Sinon le nom observé doit contenir le modèle du profil. Lorsque le nom n'est pas disponible, l'adresse autorisée et le GATT sont les seules preuves de cible : aucune identité cryptographique ne doit être revendiquée. Le champ `bluetooth_only=True` décrit ici l'intégration locale ; il n'affirme pas que toutes les révisions commerciales H6159 sont dépourvues de Wi-Fi.

Le pont expose les entrées configurées Bluetooth-only par son opération de catalogue. Le client générique peut donc présenter le nom et les capacités du nouvel appareil sans intégrer son adresse ni ses commandes dans son code. Garder l'identifiant `device` stable pour conserver sa disposition SignalRGB.

## Quand créer une nouvelle famille

Créer un transport et une session spécifiques lorsque changent l'authentification, le chiffrement, le framing, la stratégie de lecture ou les garanties de restauration. Les rattacher explicitement aux tables de familles/session et de transports de `bridge.py`, avec validation de leurs champs de configuration et tests isolés. Un simple nouveau SKU dans la famille classique ne doit pas contourner ses contrôles pour accepter un protocole inconnu.

Une famille authentifiée doit définir sa propre procédure de session et vérifier son identité avant les changements de mode/couleur. Les clés réelles, tokens et configurations privées restent hors du dépôt et des fixtures. L'existence du chiffrement E7 chez H6008 n'autorise pas à l'envoyer à un autre modèle ; inversement, l'existence d'un ancien H6159 en clair ne démontre pas que toutes ses révisions acceptent ce chemin.

## Bornes actuelles du H6159

Le profil emploie des paquets20octets, `33 05 02 R G B`, power01, brightness04, lectures AA01/04/05 et XOR final. Il ne demande aucun mode anti-fondu et n'envoie pas E7 ni AA14. L'authentification et l'identité AA14 de H6008 ne sont pas transférées à cette bande par supposition.

Les trois réponses d'état doivent être obtenues et validées **avant les changements**. Le mode autorisé est `[02,R,G,B,flag,R2,G2,B2,puis9zéros]`, avec flag00 ou01. L'APK H6159 décrit ce champ comme un booléen suivi d'un second triplet RGB ; son usage précis dans l'interface n'est pas établi ici. Le blanc initial observé portait flag01 ; une lecture ultérieure a aussi rendu le second triplet D6E1FF. Ces champs établis sont tous conservés. Les commandes RGB normales ont produit flag00.

La restitution conserve les17octets du mode original exactement. Pour vérifier une nouvelle couleur RGB normale, `matches_color_mode` compare RGB **et exige flag00** : une lecture flag01 avec le bon triplet primaire ne prouve pas que la couleur demandée soit sélectionnée. Scène, mode0D, booléen hors00/01, octets non nuls après le second triplet, checksum incorrect ou champs inconnus provoquent un refus explicite. Le second triplet peut être republié différemment par le firmware entre sessions ; une relecture immédiate identique n'est pas une preuve de persistance indéfinie.

La session respecte une extinction effectuée après sa prise de contrôle ; l'allumage initial est une décision d'acquisition explicite, pas une boucle qui annule le choix de l'utilisateur. Les libérations sont sérialisées et les délais de reprise bornés. La fermeture brutale d'un processus ou la perte physique du Bluetooth ne garantit jamais une restitution impossible à envoyer : l'erreur doit rester visible.

## Vérification hors matériel

```powershell
python -B -m unittest test_profiles test_classic_session -v
```

Les fixtures doivent couvrir au minimum : paquets exacts, identification refusée, absence de GATT, corruption de réponse, état inconnu, valeur de luminosité brute restaurée exactement, absence de couleur avant snapshot, OFF ultérieur, acquittement de libération, annulation pendant connexion et reprise bornée. Les tests de transport substituent Bleak et n'ouvrent aucune radio.
