"""Geographic helpers: distances, bounding boxes and geohashes. Pure functions."""

import math

from pydantic import BaseModel, ConfigDict, Field

EARTH_RADIUS_M = 6_371_008.8
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
_GEOHASH_BITS = (16, 8, 4, 2, 1)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class BoundingBox(BaseModel):
    """A latitude/longitude rectangle. Boxes that cross the antimeridian are not supported."""

    model_config = ConfigDict(frozen=True)

    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)

    @classmethod
    def around(cls, lat: float, lng: float, radius_km: float) -> "BoundingBox":
        """Return the box that covers a circle of ``radius_km`` around a point."""
        d_lat = math.degrees(radius_km * 1000 / EARTH_RADIUS_M)
        d_lng = d_lat / max(math.cos(math.radians(lat)), 0.01)
        return cls(
            south=max(lat - d_lat, -90),
            west=max(lng - d_lng, -180),
            north=min(lat + d_lat, 90),
            east=min(lng + d_lng, 180),
        )

    @property
    def center(self) -> tuple[float, float]:
        return (self.south + self.north) / 2, (self.west + self.east) / 2

    @property
    def radius_km(self) -> float:
        """Distance from the centre to a corner, a safe radius for APIs that take centre and radius."""
        lat, lng = self.center
        return haversine_m(lat, lng, self.north, self.east) / 1000

    def contains(self, lat: float, lng: float, *, margin_ratio: float = 0.0) -> bool:
        """Whether a point lies inside the box, which is first widened by ``margin_ratio`` of its size on each side."""
        d_lat = (self.north - self.south) * margin_ratio
        d_lng = (self.east - self.west) * margin_ratio
        return self.south - d_lat <= lat <= self.north + d_lat and self.west - d_lng <= lng <= self.east + d_lng


def geohash_encode(lat: float, lng: float, precision: int = 7) -> str:
    """Encode a point as a geohash; precision 7 is a cell of roughly 150 m."""
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    chars: list[str] = []
    bit = 0
    value = 0
    use_lng = True
    while len(chars) < precision:
        interval, coordinate = (lng_range, lng) if use_lng else (lat_range, lat)
        middle = (interval[0] + interval[1]) / 2
        if coordinate >= middle:
            value |= _GEOHASH_BITS[bit]
            interval[0] = middle
        else:
            interval[1] = middle
        use_lng = not use_lng
        if bit < len(_GEOHASH_BITS) - 1:
            bit += 1
        else:
            chars.append(_GEOHASH_ALPHABET[value])
            bit = 0
            value = 0
    return "".join(chars)
