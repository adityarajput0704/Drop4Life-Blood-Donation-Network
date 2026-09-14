import math
import os
from warnings import filters

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session, joinedload, selectinload
from backend.dependencies.__init__ import get_db
from backend.models.donor import AvailabilityEnum, Donor
from backend.models.user import User
from backend.models.blood_requests import BloodRequest
from backend.schemas.donor import DonorCreate, DonorUpdate, DonorResponse, DonorFilterParams, LocationUpdate
from backend.dependencies.auth import get_current_user
from sqlalchemy import or_, func
from backend.core.pagination import PaginationParams, PagedResponse
from backend.core.rate_limiter import limiter
from backend.core.cache import get_cached, set_cached, invalidate_cache
from datetime import date
from math import radians, sin, cos, sqrt, atan2
from fastapi import BackgroundTasks
from backend.services.notification_services import _broadcast_availability_change


router = APIRouter(prefix="/donors", tags=["Donors"])

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Returns distance in kilometers between two GPS coordinates.
    Uses the Haversine formula — accurate enough for city-level proximity.
    No external API needed.
    """
    R = 6371  # Earth radius in km
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


@router.patch("/me/location", status_code=200)
def update_my_location(
    data:         LocationUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Flutter calls this after getting GPS coordinates.
    Silently updates donor lat/long — no response body needed.
    """
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(status_code=404, detail="Donor profile not found.")

    donor.latitude  = data.latitude
    donor.longitude = data.longitude
    db.commit()

    invalidate_cache("donors:*")

    return {"message": "Location updated"}

def build_donor_response(donor: Donor, distance_km: float = None) -> dict:
    fulfilled = [d for d in donor.donations if d.status == "fulfilled"]
    total_donations = len(fulfilled)

    total_units = sum(d.units_needed for d in fulfilled)
    lives_saved = total_units

    last_donation = None
    if fulfilled:
        last = max(fulfilled, key=lambda d: d.updated_at or d.created_at)
        last_donation = (last.updated_at or last.created_at).isoformat()

    # Cooldown calculations
    today = date.today()
    is_in_cooldown = donor.is_in_cooldown
    cooldown_until = donor.cooldown_until.isoformat() if donor.cooldown_until else None
    days_remaining = 0
    if is_in_cooldown and donor.cooldown_until:
        days_remaining = (donor.cooldown_until - today).days

    return {
        "id":               donor.id,
        "blood_group":      donor.blood_group,
        "city":             donor.city,
        "age":              donor.age,
        "availability":     donor.availability,
        "is_active":        donor.is_active,
        "full_name":        donor.user.full_name,
        "email":            donor.user.email,
        "phone":            donor.user.phone,
        "total_donations":  total_donations,
        "lives_saved":      lives_saved,
        "last_donation":    last_donation,
        # Cooldown
        "is_in_cooldown":   is_in_cooldown,
        "cooldown_until":   cooldown_until,
        "days_remaining":   days_remaining,
        # Location
        "latitude":        donor.latitude,
        "longitude":       donor.longitude,
        "distance_km":     round(distance_km, 2) if distance_km is not None else None,
    }


@router.post("/register", response_model=DonorResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/minute")
def register_donor(
    request:      Request,
    donor_data: DonorCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # One user = one donor profile
    existing = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a donor profile.",
        )

    donor = Donor(
        user_id      = current_user.id,
        blood_group  = donor_data.blood_group,
        city         = donor_data.city,
        age          = donor_data.age,
        availability = donor_data.availability,
    )

    db.add(donor)
    db.commit()
    db.refresh(donor)

    # New donor registered — clear all donor list caches
    invalidate_cache("donors:*")

    return build_donor_response(donor)


@router.get("/me", response_model=DonorResponse)
def get_my_donor_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No donor profile found. Please register as a donor first.",
        )
    return build_donor_response(donor)



@router.patch("/me", response_model=DonorResponse)
def update_donor_profile(
    updates:      DonorUpdate,
    background_tasks: BackgroundTasks,          
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(status_code=404, detail="No donor profile found.")

    updates_data = updates.model_dump(exclude_unset=True)

    for field, value in updates_data.items():
        setattr(donor, field, value)

    db.commit()
    db.refresh(donor)
    invalidate_cache("donors:*")

    # Broadcast availability change to admin room
    if 'availability' in updates_data:
        background_tasks.add_task(
            _broadcast_availability_change,
            donor_id=donor.id,
            full_name=current_user.full_name,
            availability=donor.availability.value,
        )

    return build_donor_response(donor)


@router.get("/", response_model=PagedResponse[DonorResponse])
@limiter.limit("30/minute") if not os.getenv("BENCHMARK_MODE") else lambda f: f
def list_donors(
    request:    Request,
    pagination: PaginationParams = Depends(),
    filters:    DonorFilterParams = Depends(),
    db:         Session = Depends(get_db),
):
    # Skip cache if location filter is active — results are user-specific
    use_cache = not (filters.lat and filters.lng)

    cache_key = (
        f"donors:page={pagination.page}:size={pagination.page_size}:"
        f"bg={filters.blood_group}:city={filters.city}:"
        f"avail={filters.is_available}:search={filters.search}"
    )

    if use_cache:
        cached = get_cached(cache_key)
        if cached:
            return cached

    query = (
        db.query(Donor)
        .options(
            joinedload(Donor.user),
        )
        .filter(Donor.is_active == True)
    )

    if filters.blood_group:
        query = query.filter(Donor.blood_group == filters.blood_group)
    if filters.city:
        query = query.filter(Donor.city.ilike(f"%{filters.city}%"))
    if filters.is_available is not None:
        if filters.is_available is True:
         av = AvailabilityEnum.AVAILABLE
        else:
            av = AvailabilityEnum.UNAVAILABLE
        query = query.filter(Donor.availability == av)
    if filters.search:
        search_term = f"%{filters.search}%"
        query = query.join(User, Donor.user_id == User.id).filter(
            or_(User.full_name.ilike(search_term), User.phone.ilike(search_term))
        )

    # ── Proximity filter ──
    if (
        filters.lat is not None
        and filters.lng is not None
        and filters.radius_km is not None
    ):
        lat_delta = filters.radius_km / 111.0
        lng_delta = filters.radius_km / (
            111.0 * math.cos(math.radians(filters.lat))
        )

        # Cheap bounding-box pre-filter using the composite index
        query = query.filter(
        Donor.latitude.between(
            filters.lat - lat_delta,
            filters.lat + lat_delta,
        ),
        Donor.longitude.between(
            filters.lng - lng_delta,
            filters.lng + lng_delta,
        ),
    )

        # Calculate Haversine distance inside PostgreSQL
        lat1 = math.radians(filters.lat)
        lon1 = math.radians(filters.lng)

        distance = (
            6371
            * 2
           * func.atan2(
                func.sqrt(
                   func.pow(
                       func.sin(
                            (func.radians(Donor.latitude) - lat1) / 2
                        ),
                        2,
                    )
                    + func.cos(lat1)
                    * func.cos(func.radians(Donor.latitude))
                    * func.pow(
                        func.sin(
                            (func.radians(Donor.longitude) - lon1) / 2
                       ),
                        2,
                    )
                ),
                func.sqrt(
                    1
                    - (
                        func.pow(
                            func.sin(
                                (func.radians(Donor.latitude) - lat1) / 2
                            ),
                            2,
                        )
                        + func.cos(lat1)
                        * func.cos(func.radians(Donor.latitude))
                        * func.pow(
                            func.sin(
                                (func.radians(Donor.longitude) - lon1) / 2
                            ),
                            2,
                        )
                    )
                ),
            )
        ).label("distance_km")

        query = (
           query
            .add_columns(distance)
            .filter(distance <= filters.radius_km)
            .order_by(distance)
        )

        total = query.count()

        paginated = (
           query
           .offset(pagination.offset)
            .limit(pagination.page_size)
            .all()
        )

        items = [
           build_donor_response(donor, distance)
           for donor, distance in paginated
       ]

    else:
        total = query.count()

        paginated = (
            query
            .offset(pagination.offset)
            .limit(pagination.page_size)
            .all()
        )

        items = [
            build_donor_response(donor)
            for donor in paginated
        ]

    result = PagedResponse.create(items=items, total=total, params=pagination)

    if use_cache:
        set_cached(cache_key, result.model_dump(), ttl_seconds=60)

    return result