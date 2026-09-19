import QtQuick
import QtQuick.Controls

Item {
    anchors.fill: parent
    Column {
        x: 12
        y: 12
        width: parent.width - 24
        spacing: 14
        Text {
            width: parent.width
            color: "white"
            font.pixelSize: 22
            text: "Govee Bluetooth Only"
            wrapMode: Text.WordWrap
        }
        Text {
            width: parent.width
            color: "#cccccc"
            font.pixelSize: 15
            text: "Démarrez le pont Bluetooth local. Les appareils autorisés apparaissent automatiquement dans Appareils. Réglez ensuite Canvas ou Forced, la luminosité si elle est prise en charge, et le comportement à l’arrêt. Les choix dépendent des capacités déclarées par le profil."
            wrapMode: Text.WordWrap
        }
        Text {
            width: parent.width
            color: "#cccccc"
            font.pixelSize: 15
            text: "Une seule zone RGB par appareil, jusqu’à 10 mises à jour par seconde. Les transitions normales de l’appareil sont conservées. Seuls les profils déclarés par le pont sont acceptés ; les appareils adressables ne sont pas pris en charge ici."
            wrapMode: Text.WordWrap
        }
        Text {
            width: parent.width
            color: "#cccccc"
            font.pixelSize: 15
            text: "Le catalogue est actualisé toutes les 5 secondes. Si le pont est indisponible, les couleurs sont suspendues et une alerte apparaît dans l’appareil. Aucune commande LAN n’est utilisée."
            wrapMode: Text.WordWrap
        }
        Button {
            text: "Actualiser le catalogue local"
            onClicked: discovery.Refresh()
        }
    }
}
