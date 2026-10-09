# Seguimiento: protocolo y control de válvulas

Estado a 2026-10-09. Contexto en [PROTOCOL.md](../PROTOCOL.md).

## Objetivo

1. Confirmar con capturas reales lo que se dedujo de la app.
2. Identificar los registros sin documentar.
3. Ver si se pueden abrir o cerrar las válvulas sin termostato (baños).

## Lo que sabemos

- **Equipos:** el CK1080 es la pasarela BUSing↔ZBUS. Tiene el control PI y los relés de las válvulas (ficha técnica de Orkli). El TermoWifi es un nodo del bus BUSing, vinculado a la zona 1.
- **Dirección `0x01`:** casi seguro es el CK1080. El puerto 12345 del TermoWifi hace de puente con el bus BUSing. *Sin confirmar.*
- **Firmware:** no está publicado.
  - TermoWifi: el AP en `192.168.4.1` apunta a un ESP8266/ESP32 (sin confirmar).
  - CK1080: probablemente tiene protección de lectura.
  - La lógica de las válvulas está en el CK1080, así que el firmware del TermoWifi aportaría poco para este objetivo.
- **Otra integración:** [wachino/orkli_wifi_thermostat](https://github.com/wachino/orkli_wifi_thermostat) confirma la trama, el checksum, el puerto y el mapa de registros. También usa `CMD 0x0A` (`C_POLLING`) y lee `Instal.dat` del FTP de la nube.
- **Registros pendientes:** `0x33`, `0x34` y `0x36` aparecían en las capturas antiguas y la app no los usa. Son candidatos a estado de válvulas o salidas.

## Herramienta de captura

`tools/termowifi_sniffer.py` (solo librería estándar de Python):

```bash
# Pasivo: solo lee del socket
python3 tools/termowifi_sniffer.py <IP_TERMOWIFI> --out captura.jsonl

# Solo cambios de valor, más legible para sesiones largas
python3 tools/termowifi_sniffer.py <IP_TERMOWIFI> --changes-only --out captura.jsonl

# Además envía peticiones de lectura (volcado, C_POLLING, zonas 0..7)
python3 tools/termowifi_sniffer.py <IP_TERMOWIFI> --probe --out captura.jsonl
```

- Al salir (Ctrl+C) imprime un resumen con cada registro visto y sus valores distintos.
- `captura.jsonl` guarda una trama por línea con marca de tiempo, para analizarla después.
- **Conexiones simultáneas:** un cliente local en el puerto 12345 convive con la app del móvil, que va por la nube. Falta confirmar que admita dos clientes locales a la vez (Home Assistant y el script). Comprobación: con el script en marcha, cambiar una consigna desde HA y ver que aparece en ambos. Si HA pierde la conexión, desactiva la integración mientras capturas.

## Plan

### Fase 1: escucha pasiva (sin riesgo)

- [ ] Capturar un día entero con calefacción activa (`--changes-only --out`).
- [ ] Apuntar a mano los momentos conocidos: cuándo pide cada termostato, cuándo se abre la válvula de los baños, cuándo arranca la bomba o la válvula general.
- [ ] Correlacionar `0x33`, `0x34`, `0x36` y cualquier registro nuevo con esos momentos.
- [ ] Anotar si aparecen tramas con orígenes distintos de `01`/`FE`, que serían otros nodos BUSing.

### Fase 2: lecturas (riesgo bajo)

- [ ] Ejecutar `--probe` y ver qué zonas responden. ¿Aparecen los baños como zonas sin termostato?
- [ ] Ver qué responde a `C_POLLING` en las direcciones `01` y `FF`.
- [ ] Descargar `Instal.dat` con las credenciales de la app (FTP de la nube de Orkli). Ver cómo están definidas las salidas y si los baños tienen dirección o salida propia.

### Fase 3: escrituras (solo con supervisión)

Solo con alguien delante de los colectores y con valores que se puedan deshacer:

- [ ] Escribir estado o consigna en una zona sin termostato y observar los relés del CK1080.
- [ ] Escribir en los registros identificados en la fase 1, empezando por el valor que ya tienen.
- [ ] Documentar en `PROTOCOL.md` lo confirmado y retirar las marcas *unconfirmed*.

### Opcional: firmware del TermoWifi

Solo si las fases anteriores no bastan:

- [ ] Identificar el chip abriendo el TermoWifi (sin tensión).
- [ ] Si es un ESP, leer la flash con `esptool read_flash` mediante un adaptador serie de 3,3 V. Guardar copia antes de nada y no escribir.

## Pendiente de verificar en Home Assistant (v0.2.0)

- [ ] Las zonas reales se descubren en el primer minuto.
- [ ] Las entidades que venían de la detección antigua y no son zonas reales dejan de actualizarse. Hay que borrarlas.
- [ ] Encender y apagar, cambiar consigna y cambiar calor/frío se reflejan en HA.
- [ ] Si algún cambio no se refleja, revisar el log en `debug`. Las confirmaciones (`3B FE 01 01 …`) ya no actualizan el estado.
