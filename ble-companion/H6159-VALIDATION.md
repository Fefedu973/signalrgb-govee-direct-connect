# H6159 — validation matérielle bornée

Date : 19septembre2026. Profil `h6159-classic-v1`. Modèle réellement identifié : H6159, firmware1.07.02, révisions matérielles rapportées1.00.01/1.03.05. Cette fiche ne contient ni adresse Bluetooth, ni identifiant individuel, ni configuration privée.

## Résultat

Trois commandes couleur et luminosité ont été relues correctement, puis l'état initial a été restitué exactement. L'essai a été répété après prise en charge du second triplet RGB, avec le même succès et une durée totale de16,672s, le19septembre2026 à18:31:24UTC. L'utilisateur a confirmé : **« Oui, tout fonctionne ».** Aucune commande anti-fondu H6008, E7, mode05/01 ou AA14 n'a été envoyée. Les réponses ci-dessous sont des preuves de protocole, accompagnées de cette confirmation visuelle ; elles ne mesurent pas la cadence optique.

| Étape | RGB demandé et relu | Luminosité demandée | Octet04 relu | Power relu |
|---|---|---|---|---|
|1|255,0,0|40%|102|1|
|2|0,255,0|60%|153|1|
|3|0,0,255|80%|204|1|

État initial et final identiques lors de la reprise : power1, luminosité brute255 (100%), mode17octets `02ffffff01d6e1ff000000000000000000`. Le contrôle final de la session a indiqué `restored_exact=true`, sans erreur de contrôle ni de restauration. Le premier essai avait déjà confirmé la restauration du mode `02ffffff01000000000000000000000000`. L'extinction matérielle n'a pas été testée séparément dans ces essais ; le domaine power est sourcé et son état initial allumé a été restitué.

## Lectures et transport réellement observés

Service GATT `00010203-0405-0607-0809-0a0b0c0d1910`, notifications `…2b10`, écriture `…2b11`. La caractéristique d'écriture annonce read et write-without-response. Les lectures ont toutefois répondu avec ATT write-request (`response=True`). La première AA01 n'avait pas répondu juste après l'abonnement ; sa reprise après AA04/AA05 a réussi. Le transport réessaie désormais chaque lecture une fois, au maximum3s par tentative. L'hypothèse d'un délai de stabilisation GATT explique l'observation, mais sa cause interne n'est pas démontrée.

Fixtures complètes en clair,20octets et XOR valide :

```text
power initial       aa010100000000000000000000000000000000aa
brightness initial  aa04ff0000000000000000000000000000000051
mode initial        aa0502ffffff0100000000000000000000000053
```

L'APK Govee Home7.6.21, classe `com.govee.h6159.ble.SubModeColor`, écrit `[02,R,G,B,bool,R2,G2,B2]` et relit ces mêmes champs. Le flag01 observé n'est donc pas du padding à supprimer. Les écritures RGB simples ont produit flag00 ; la restauration du mode original avec flag01 a été confirmée par relecture immédiate. L'usage fonctionnel précis du booléen n'est pas établi ici.

Lors de la session suivante, la lecture AA05 a rendu `aa0502ffffff01d6e1ff0000000000000000009b` : même blanc primaire et flag01, second triplet D6E1FF. La validation initiale trop restrictive a arrêté cette reprise **avant toute écriture**. Le profil a alors été élargi aux trois octets secondaires explicitement décrits par l'APK, avec conservation des17octets et refus des champs inconnus après ce triplet. La nouvelle répétition a restauré ce second snapshot exactement et reçu la confirmation utilisateur citée plus haut. Cela ne prouve pas que le firmware conserve indéfiniment les champs d'un snapshot : les vérifications de restauration étaient immédiates. Une nouvelle couleur RGB ne satisfait le matcher qu'avec flag00 et le triplet primaire demandé ; flag01 n'est jamais accepté comme preuve d'une couleur RGB normale.

## Portée et reproduction

Le profil est non adressable et utilise le transport Bluetooth local. Cela n'affirme pas que toutes les variantes commerciales H6159 sont dépourvues de Wi-Fi ou d'authentification. L'identification de la cible combine adresse autorisée, nom annoncé et GATT ; aucune identité cryptographique du modèle n'est revendiquée.

Avant tout contrôle : identifier la cible exacte, obtenir AA01/04/05, vérifier longueur/checksum/mode supporté, puis seulement prendre le contrôle. Conserver luminosité brute et mode17octets ; ne jamais reconstruire l'état initial depuis un pourcentage arrondi. Le script de configuration fournit les profils disponibles sans ouvrir Bluetooth.

Les18tests de `test_profiles.py` passent hors matériel : fixtures RGB, entrées invalides, luminosité brute exacte pour256valeurs, fixtures réelles flag01 avec second triplet nul et D6E1FF, matcher final_rgb strict flag00, registre, contrôle de cible/GATT, ATT response=True, retry unique et timeout borné, restauration exacte des champs, refus des opcodes H6008. Les tests de session sont séparés.

Sources primaires : [recherche H6159 historique](https://github.com/jurassic-marc/govee-h6159-light-strip-reverse-engineer/tree/b76ea47acba786b25a38dd9d48a102e1b4eda264), [bibliothèque BLE déclarant H6159 testé](https://github.com/softgrass/govee-api-ble/blob/59e77714bd5764ceeed49a624c385f1494d105af/src/govee_api_ble/device.py), APK Govee Home7.6.21 présent localement. Preuves locales de cet essai : `work/govee/h6159-read-only-probe.json` et `work/govee/h6159-hardware-validation.json`. Ces fichiers privés ne sont pas requis pour installer le profil ; la présente fiche n'en reproduit que les résultats non identifiants.
