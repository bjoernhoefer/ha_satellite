# Home Assistant einbinden

`ha_satellite` läuft ohne Authentifizierung im privaten Netz und ist über
WireGuard von beiden Home-Assistant-Instanzen direkt erreichbar. Ersetze
`<container>` durch die jeweils aus Sicht der HA-Instanz erreichbare
WireGuard-Adresse des Hosts `nzbpi` (Container-Port: `6060`).

## Wien (`192.168.188.12:8123`)

```yaml
camera:
  - platform: mjpeg
    name: Satellit Wien
    mjpeg_url: http://192.168.188.13:6060/regions/wien/mjpeg

  - platform: generic
    name: Satellit Wien Standbild
    still_image_url: http://192.168.188.13:6060/regions/wien/latest.png
    framerate: 0.017  # ~ alle 60s neu laden
```

## Porto Cristo (`192.168.88.77:8123`)

Über WireGuard erreicht die Instanz in Porto Cristo den Host `nzbpi` unter
seiner WireGuard-Adresse (Beispiel unten mit der IM übrigen Dokument
verwendeten LAN-Adresse — auf die tatsächliche WireGuard-IP anpassen,
sobald das Tunnel-Subnetz feststeht):

```yaml
camera:
  - platform: mjpeg
    name: Satellit Mallorca
    mjpeg_url: http://192.168.188.13:6060/regions/mallorca/mjpeg

  - platform: generic
    name: Satellit Mallorca Standbild
    still_image_url: http://192.168.188.13:6060/regions/mallorca/latest.png
    framerate: 0.017
```

> **Hinweis:** Die konkrete WireGuard-IP von `nzbpi` aus Sicht des
> Porto-Cristo-Standorts ist in dieser Dokumentation als offener Punkt
> markiert (siehe HISTORY.md) — bitte die tatsächliche Tunnel-Adresse
> eintragen, sobald sie feststeht.

## Lovelace-Karte (Picture-Entity)

```yaml
type: picture-entity
entity: camera.satellit_wien
camera_view: live
name: Satellit Wien
show_state: false
```

Für eine kombinierte Ansicht beider Standorte eignet sich eine
`picture-glance`- oder `grid`-Karte mit beiden `camera.*`-Entitäten.

## Beispiel-Automation: Warnung bei veraltetem Bild

Warnt, wenn das neueste Bild deutlich älter ist als der Aufnahmetakt der
Quelle (Rapid Scan 5 min, MTG FCI 10 min, 0° 15 min; plus einige Minuten
Lieferverzögerung). Die Schwelle von 30 Minuten passt für alle Quellen.

```yaml
automation:
  - alias: "Satellitenbild Wien veraltet"
    trigger:
      - platform: time_pattern
        minutes: "/5"
    condition:
      - condition: template
        value_template: >
          {{ (now().timestamp() - states('sensor.satellit_wien_letztes_bild_alter') | float(0)) > 0 }}
        # Alternativ direkt gegen /api/status prüfen, sofern ein REST-Sensor
        # (siehe unten) das Alter des neuesten Bildes bereitstellt.
    action:
      - service: notify.persistent_notification
        data:
          title: "Satellitenbild Wien veraltet"
          message: >
            Das neueste Satellitenbild für Wien ist älter als 30 Minuten.
            Bitte /api/status auf dem ha_satellite-Host prüfen.
```

Empfohlen: ein REST-Sensor, der `latest_age_seconds` aus
`GET /api/status` ausliest, z. B.:

```yaml
sensor:
  - platform: rest
    name: Satellit Wien Bildalter
    resource: http://192.168.188.13:6060/api/status
    value_template: "{{ (value_json.wien.latest_age_seconds | float(0)) | round(0) }}"
    unit_of_measurement: s
    scan_interval: 60
```

Damit lässt sich die obige Automation direkt gegen
`states('sensor.satellit_wien_bildalter') | float(0) > (<intervall_min> * 2 * 60)`
prüfen.
