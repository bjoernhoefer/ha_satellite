# Home Assistant integration

`ha_satimage` runs without authentication and is meant for a private
network. Replace `<host>` with the address of the Docker host running
ha_satimage as seen from your Home Assistant instance (container port:
`6060`). If Home Assistant runs at another site, any network path that
makes `<host>:6060` reachable (VPN, routed LAN, …) works.

## Example: Vienna

```yaml
camera:
  - platform: mjpeg
    name: Satellite Vienna
    mjpeg_url: http://<host>:6060/regions/wien/mjpeg

  - platform: generic
    name: Satellite Vienna still
    still_image_url: http://<host>:6060/regions/wien/latest.png
    framerate: 0.017  # reload ~ every 60 s
```

## Example: Mallorca

```yaml
camera:
  - platform: mjpeg
    name: Satellite Mallorca
    mjpeg_url: http://<host>:6060/regions/mallorca/mjpeg

  - platform: generic
    name: Satellite Mallorca still
    still_image_url: http://<host>:6060/regions/mallorca/latest.png
    framerate: 0.017
```

## Lovelace card (picture entity)

```yaml
type: picture-entity
entity: camera.satellite_vienna
camera_view: live
name: Satellite Vienna
show_state: false
```

For a combined view of several regions use a `picture-glance` or `grid`
card with multiple `camera.*` entities.

## Example automation: warn about a stale image

First a REST sensor that reads `latest_age_seconds` from `GET /api/status`:

```yaml
sensor:
  - platform: rest
    name: Satellite Vienna image age
    resource: http://<host>:6060/api/status
    value_template: "{{ (value_json.wien.latest_age_seconds | float(0)) | round(0) }}"
    unit_of_measurement: s
    scan_interval: 60
```

Then warn when the newest image is clearly older than the source's scan
cycle (Rapid Scan 5 min, MTG FCI 10 min, 0° 15 min; plus a few minutes of
delivery delay). A threshold of 30 minutes suits all sources.

```yaml
automation:
  - alias: "Satellite image Vienna stale"
    trigger:
      - platform: numeric_state
        entity_id: sensor.satellite_vienna_image_age
        above: 1800
    action:
      - service: notify.persistent_notification
        data:
          title: "Satellite image Vienna stale"
          message: >
            The newest satellite image for Vienna is older than 30 minutes.
            Please check /api/status on the ha_satimage host.
```
