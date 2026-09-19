# H6008 : cadence mesurée et réglage retenu

Test matériel du 19 septembre 2026 sur trois H6008, matériel 1.07.03, firmware 1.01.25, avec le même PC Windows et son adaptateur Bluetooth. Arc-en-ciel HSV continu, tour de teinte en 4 secondes, décalage d'un tiers entre ampoules, saturation 100 %, valeur 64/255. Deux paliers de 10 secondes, sans changer la luminosité système des ampoules.

| Demande | Ampoule | Écritures couleur terminées | Débit mesuré | Intervalle médian | Intervalle p95 |
|---|---|---:|---:|---:|---:|
|10 images/s|A|99|9,763/s|94 ms|140,15 ms|
|10 images/s|B|100|9,862/s|94 ms|140,10 ms|
|10 images/s|C|100|9,862/s|94 ms|140,00 ms|
|20 images/s|A|141|13,904/s|63 ms|79,70 ms|
|20 images/s|B|143|14,101/s|63 ms|94,00 ms|
|20 images/s|C|143|14,101/s|63 ms|92,30 ms|

La fenêtre de mesure inclut environ 0,14 seconde pour terminer la dernière écriture. Les compteurs portent uniquement sur les écritures BLE couleur terminées côté hôte ; ils excluent l'initialisation et les lectures d'état. Ce n'est pas une mesure optique de chaque image affichée. Les 256 horodatages conservés par ampoule couvrent entièrement chaque palier. La coalescence remplace les couleurs devenues anciennes par la dernière demandée : l'écart entre demande et débit ne prouve pas une perte radio.

L'utilisateur a jugé les deux paliers fluides et n'a pas vu de différence entre eux. Le réglage retenu reste donc 100 ms, soit au plus10 images/s en production. Le seuil 50 ms nécessite `experimental_20fps:true` et reste expérimental : ce PC n'a pas atteint 20 écritures/s dans cette configuration. Ce résultat ne mesure pas une limite universelle du H6008 ou du Bluetooth.

Les trois identités AA14 ont été vérifiées avant la séquence, et les états couleur/Kelvin/luminosité/allumage initiaux ont été restaurés puis relus avec succès sur les trois ampoules. Le mode BLE 05 supprime le fondu observé avec le mode 0D. Une simple activation 05 suivie de `colorwc` LAN ne suffit pas : les 12 lectures du test précédent revenaient toutes à 0D.

Deux premiers essais du benchmark avaient été interrompus avant les couleurs parce que la troisième ampoule n'apparaissait pas dans le scan. Le correctif évite de compter plusieurs échecs sur un même cache et permet, sur Windows et pour la liste autorisée uniquement, la connexion directe à l'adresse connue. Le benchmark réussi inclut ce correctif. Aucun appairage, reset d'adaptateur ou flash n'a été effectué.

La clé OEM et les adresses réelles sont exclues de ce paquet. Les preuves détaillées restent locales ; `mesures.json` fournit les métriques anonymisées partageables.
