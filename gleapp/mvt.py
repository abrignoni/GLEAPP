"""A small, dependency-free decoder for Mapbox Vector Tiles (MVT).

Enough of the spec to draw a locator map: it reads each layer's name, extent and
the geometry of every feature, in tile-local integer coordinates. Feature
attributes are skipped, because the static renderer colours features by their
layer, not by their tags. No third-party protobuf runtime is used, so this works
on every platform GLEAPP ships on with nothing to compile.

Reference: Mapbox Vector Tile specification 2.1
(github.com/mapbox/vector-tile-spec/blob/master/2.1/vector_tile.proto).
"""

from __future__ import annotations

# Geometry command ids (MVT 2.1, section 4.3.5).
_MOVE_TO = 1
_LINE_TO = 2
_CLOSE_PATH = 7

# Feature geometry types (field 3 of a Feature).
POINT = 1
LINESTRING = 2
POLYGON = 3


def _uvarint(buf: bytes, p: int) -> tuple[int, int]:
    """A base-128 varint at ``buf[p]``; returns the value and the next offset."""
    result = 0
    shift = 0
    while True:
        b = buf[p]
        p += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, p
        shift += 7


def _zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _decode_geometry(cmds: bytes) -> list[list[tuple[int, int]]]:
    """The command stream of one feature into a list of point rings/paths.

    Each returned path is a list of (x, y) in tile units (0..extent). A point
    feature yields one single-point path per point; a line yields one path per
    line; a polygon yields one path per ring.
    """
    paths: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    x = y = 0
    p = 0
    n = len(cmds)
    while p < n:
        cmd_int, p = _uvarint(cmds, p)
        cmd = cmd_int & 0x7
        count = cmd_int >> 3
        if cmd == _MOVE_TO:
            for _ in range(count):
                dx, p = _uvarint(cmds, p)
                dy, p = _uvarint(cmds, p)
                x += _zigzag(dx)
                y += _zigzag(dy)
                if cur:
                    paths.append(cur)
                cur = [(x, y)]
        elif cmd == _LINE_TO:
            for _ in range(count):
                dx, p = _uvarint(cmds, p)
                dy, p = _uvarint(cmds, p)
                x += _zigzag(dx)
                y += _zigzag(dy)
                cur.append((x, y))
        elif cmd == _CLOSE_PATH:
            if cur:
                paths.append(cur)
                cur = []
        else:
            break                                  # unknown command: stop this feature
    if cur:
        paths.append(cur)
    return paths


def _parse_value(buf: bytes):
    """One entry of a layer's value table: the first field that carries one wins.

    A value is a message with exactly one field set, of whichever type it is. Only
    the string case is needed for labels, but the numbers are cheap to keep and a
    caller that reads a kind or a rank should not have to re-parse the tile.
    """
    p = 0
    n = len(buf)
    while p < n:
        tag, p = _uvarint(buf, p)
        field, wire = tag >> 3, tag & 0x7
        if wire == 2:
            ln, p = _uvarint(buf, p)
            chunk = buf[p:p + ln]
            p += ln
            if field == 1:                          # string
                return chunk.decode("utf-8", "replace")
        elif wire == 0:
            val, p = _uvarint(buf, p)
            if field == 4:                          # int64
                return val
            if field == 5:                          # uint64
                return val
            if field == 6:                          # sint64
                return _zigzag(val)
            if field == 7:                          # bool
                return bool(val)
        elif wire == 5:
            p += 4
        elif wire == 1:
            p += 8
        else:
            return None
    return None


def _parse_feature(buf: bytes) -> dict | None:
    gtype = 0
    geom = b""
    tags: list[int] = []
    p = 0
    n = len(buf)
    while p < n:
        tag, p = _uvarint(buf, p)
        field, wire = tag >> 3, tag & 0x7
        if wire == 0:
            val, p = _uvarint(buf, p)
            if field == 3:
                gtype = val
        elif wire == 2:
            ln, p = _uvarint(buf, p)
            chunk = buf[p:p + ln]
            p += ln
            if field == 4:
                geom = chunk
            elif field == 2:                        # packed key/value index pairs
                q = 0
                while q < len(chunk):
                    idx, q = _uvarint(chunk, q)
                    tags.append(idx)
        elif wire == 5:
            p += 4
        elif wire == 1:
            p += 8
        else:                                      # unknown wire type: give up on this feature
            return None
    if not gtype or not geom:
        return None
    return {"type": gtype, "paths": _decode_geometry(geom), "tag_indices": tags}


def _parse_layer(buf: bytes) -> dict:
    name = ""
    extent = 4096
    features: list[dict] = []
    keys: list[str] = []
    values: list = []
    p = 0
    n = len(buf)
    while p < n:
        tag, p = _uvarint(buf, p)
        field, wire = tag >> 3, tag & 0x7
        if wire == 0:
            val, p = _uvarint(buf, p)
            if field == 5:
                extent = val
        elif wire == 2:
            ln, p = _uvarint(buf, p)
            chunk = buf[p:p + ln]
            p += ln
            if field == 1:
                name = chunk.decode("utf-8", "replace")
            elif field == 2:
                feat = _parse_feature(chunk)
                if feat:
                    features.append(feat)
            elif field == 3:
                keys.append(chunk.decode("utf-8", "replace"))
            elif field == 4:
                values.append(_parse_value(chunk))
        elif wire == 5:
            p += 4
        elif wire == 1:
            p += 8
        else:
            break
    # A feature's tags are indices into the layer's key and value tables, and the
    # tables can appear after the features that use them, so they are resolved once
    # the whole layer is read rather than as each feature is parsed.
    for feat in features:
        indices = feat.pop("tag_indices", [])
        attrs = {}
        for i in range(0, len(indices) - 1, 2):
            k, v = indices[i], indices[i + 1]
            if k < len(keys) and v < len(values):
                attrs[keys[k]] = values[v]
        feat["tags"] = attrs
    return {"name": name, "extent": extent, "features": features}


def decode(data: bytes) -> dict[str, dict]:
    """Decode a decompressed MVT tile into ``{layer_name: {extent, features}}``.

    ``features`` is a list of ``{"type": 1|2|3, "paths": [[(x, y), ...], ...]}``.
    Malformed input yields whatever was read before the trouble, never an
    exception, so one bad tile cannot abort a report.
    """
    layers: dict[str, dict] = {}
    p = 0
    n = len(data)
    try:
        while p < n:
            tag, p = _uvarint(data, p)
            field, wire = tag >> 3, tag & 0x7
            if wire == 2:
                ln, p = _uvarint(data, p)
                chunk = data[p:p + ln]
                p += ln
                if field == 3:
                    layer = _parse_layer(chunk)
                    if layer["name"]:
                        layers[layer["name"]] = layer
            elif wire == 0:
                _, p = _uvarint(data, p)
            elif wire == 5:
                p += 4
            elif wire == 1:
                p += 8
            else:
                break
    except IndexError:
        pass
    return layers
