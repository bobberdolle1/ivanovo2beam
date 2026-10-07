# Getting a generated level into BeamNG.drive

## Requirements

* **BeamNG.drive 0.39.x**, installed through Steam.
* **Steam running.** BeamNG is a Steam title: launching `BeamNG.drive.exe`
  directly, without Steam, exits immediately with code 0 and loads nothing.
* Windows. The generator runs anywhere, but BeamNG itself is Windows.

## Where BeamNG keeps levels

Modern BeamNG versions moved the user data folder out of `Documents`. On
0.39.x it is here:

```
%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\
    levels\      <-- loose levels, the development path
    mods\        <-- packaged mod zips
```

`ivanovo2beam install` locates this automatically, and you can override it:

```bash
ivanovo2beam install --config configs/ivanovo_mvp.yaml
ivanovo2beam install --config configs/ivanovo_mvp.yaml --beamng "D:\BeamNG\current"
```

You can also set `BEAMNG_USERDATA` in the environment.

## Two ways to install

### Loose folder (recommended while iterating)

```bash
ivanovo2beam build   --config configs/ivanovo_mvp.yaml
ivanovo2beam install --config configs/ivanovo_mvp.yaml
```

This copies `build/levels/ivanovo_mvp/` into
`…\current\levels\ivanovo_mvp\`. BeamNG reloads a loose level folder as soon as
you re-enter the level, so this is the workflow to use while tuning.

### Mod zip (recommended for sharing)

```bash
ivanovo2beam build --config configs/ivanovo_mvp.yaml
# then copy build/ivanovo_mvp.zip into the mods folder
```

A BeamNG map archive needs nothing at its root except `levels/<id>/…`. Our
archives contain exactly that: 13 entries, all JSON plus one `.ter`.

Installing via Steam Workshop also works, with one caveat from BeamNG's EULA
(§11.4): published mods "should not set up special usage terms or license fees".

## Loading it

1. Start BeamNG.
2. **Single player → Freeroam**, or the level selector.
3. Choose **"Ivanovo 1:1 - City Centre (MVP)"**.
4. Spawn — the spawn point is on a road near Ivanovo city centre, not in a field.

## Verifying it worked

Signs the level loaded correctly:

* Terrain is visible with a road decal lying flat on it, and driving along the
  decal feels smooth rather than washboard-like.
* The in-game map view covers about 5 km across.
* Driving the main street for its full length takes roughly the same time as
  driving 5 km at 60 km/h — about five minutes.
* The ground under the car is the terrain, not a floating mesh: get out of the
  car and walk onto the road; the surface continues.

If the level fails to load, check `…\current\beamng.log` and open an issue with
the relevant lines.

## A sanity check on scale

The whole point of the project is that distances are real. The strongest quick
test available in the MVP sector:

1. Spawn at the default drop point.
2. Note the in-game distance readout or use the map view.
3. Drive the length of the main road across the sector.

It should read close to **5.12 km** for the full crossing. The projection's
worst-case error is 0.0106 %, i.e. about half a metre over that distance.

## Removing it

```powershell
Remove-Item -Recurse "$env:LOCALAPPDATA\BeamNG\BeamNG.drive\current\levels\ivanovo_mvp"
Remove-Item "$env:LOCALAPPDATA\BeamNG\BeamNG.drive\current\mods\ivanovo_mvp.zip"
```

The generator never modifies any file that ships with the game.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Game exits instantly, nothing happens | Steam is not running |
| Level appears in the list but loads to a black screen | Run `ivanovo2beam validate`; a `.ter`/`.terrain.json` mismatch does this |
| Terrain renders magenta or black | A material texture path did not resolve — re-run `validate --beamng <install root>` |
| Level is listed twice | Both a loose folder and a mod zip are installed; remove one |
| Roads visibly float or sink | Decal nodes were spaced too far apart for the terrain's curvature; lower `max_decal_node_spacing_m` in `build_level` |
