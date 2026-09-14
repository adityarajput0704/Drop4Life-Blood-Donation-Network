import math
from dns import query
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, BackgroundTasks
from sqlalchemy.orm import Session, joinedload
from typing import Optional
from backend.dependencies.__init__ import get_db
from backend.models.blood_requests import BloodRequest, RequestStatusEnum
from backend.models.donor import Donor
from backend.models.hospitals import Hospital
from backend.models.user import User
from backend.schemas.blood_request import (
    BloodRequestCreate,
    BloodRequestResponse,
    BloodRequestListResponse,
    RequestFilterParams
)
from backend.dependencies.auth import get_current_user, get_current_hospital, require_admin
from backend.utils.blood_compatibility import get_compatible_donor_groups
from sqlalchemy import or_
from backend.core.pagination import PaginationParams, PagedResponse
from backend.core.rate_limiter import limiter
from backend.core.cache import get_cached, set_cached, invalidate_cache
from backend.services.notification_services import (
    notify_request_created,
    notify_request_accepted,
    notify_donation_fulfilled,
)
from math import radians, sin, cos, sqrt, atan2
from backend.models.hospitals import Hospital

router = APIRouter(prefix="/blood-requests", tags=["Blood Requests"])

def _haversine(lat1, lon1, lat2, lon2):
    R = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def build_request_response(req: BloodRequest) -> dict:
    return {
        "id":             req.id,
        "blood_group":    req.blood_group,
        "units_needed":   req.units_needed,
        "patient_name":   req.patient_name,
        "urgency":        req.urgency,
        "status":         req.status,
        "notes":          req.notes,
        "created_at":     req.created_at,
        "hospital_name":  req.hospital.name,
        "hospital_city":  req.hospital.city,
        "hospital_phone": req.hospital.phone,
        "donor_name":     req.donor.user.full_name if req.donor else None,
        "donor_phone":    req.donor.user.phone     if req.donor else None,
        # Location — for Flutter map screen
        "hospital_lat":   req.hospital.latitude,
        "hospital_lng":   req.hospital.longitude,
    }


# ── HOSPITAL ──────────────────────────────────────────────────────────────────

@router.post("/", response_model=BloodRequestResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def create_blood_request(
    request:           Request,
    data:              BloodRequestCreate,
    background_tasks:  BackgroundTasks,              # ← inject this
    current_hospital:  Hospital = Depends(get_current_hospital),
    db:                Session = Depends(get_db),
):
    blood_request = BloodRequest(
        hospital_id  = current_hospital.id,
        blood_group  = data.blood_group,
        units_needed = data.units_needed,
        patient_name = data.patient_name,
        urgency      = data.urgency,
        notes        = data.notes,
    )
    db.add(blood_request)
    db.commit()
    db.refresh(blood_request)

    invalidate_cache("blood_requests:*")

    # Add background task — runs after response is sent
    background_tasks.add_task(
    notify_request_created,
    request_id=blood_request.id,
    hospital_name=current_hospital.name,
    blood_group=blood_request.blood_group.value,
    urgency=blood_request.urgency.value,
)

    return build_request_response(blood_request)


@router.get("/my-requests", response_model=PagedResponse[BloodRequestResponse])
def hospital_my_requests(
    pagination:       PaginationParams = Depends(),
    status:           Optional[str]    = Query(None),
    blood_group:      Optional[str]    = Query(None),
    current_hospital: Hospital         = Depends(get_current_hospital),
    db:               Session          = Depends(get_db),
):
    """Hospital sees their own requests with pagination and filtering."""
    query = db.query(BloodRequest).filter(
        BloodRequest.hospital_id == current_hospital.id
    )

    if status:
        query = query.filter(BloodRequest.status == status.lower())
    if blood_group:
        query = query.filter(BloodRequest.blood_group == blood_group)

    total = query.count()
    items = (
        query
        .order_by(BloodRequest.created_at.desc())
        .offset(pagination.offset)
        .limit(pagination.page_size)
        .all()
    )

    return PagedResponse.create(
        items=[build_request_response(r) for r in items],
        total=total,
        params=pagination,
    )


@router.post("/{request_id}/cancel", response_model=BloodRequestResponse)
def cancel_blood_request(
    request_id:       int,
    current_hospital: Hospital = Depends(get_current_hospital),
    db:               Session = Depends(get_db),
):
    """
    Hospital cancels their own request.
    Cannot cancel already fulfilled requests.
    """
    blood_request = db.query(BloodRequest).filter(BloodRequest.id == request_id).first()
    if not blood_request:
        raise HTTPException(status_code=404, detail="Blood request not found.")

    if blood_request.hospital_id != current_hospital.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only cancel your own requests.",
        )

    # State guard — prevent illogical transitions
    if blood_request.status == RequestStatusEnum.FULFILLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot cancel a request that has already been fulfilled.",
        )
    if blood_request.status == RequestStatusEnum.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Request is already cancelled.",
        )

    blood_request.status = RequestStatusEnum.CANCELLED
    db.commit()
    db.refresh(blood_request)

    invalidate_cache("blood_requests:*")

    return build_request_response(blood_request)


# ── DONOR ─────────────────────────────────────────────────────────────────────
# backend/routers/blood_requests.py
# Add BEFORE the GET "/" route

@router.post("/{request_id}/cancel-acceptance", response_model=BloodRequestResponse)
def cancel_my_acceptance(
    request_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Donor cancels their own acceptance — returns request to OPEN."""
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(status_code=403, detail="Only donors can cancel acceptance.")

    blood_request = db.query(BloodRequest).filter(
        BloodRequest.id == request_id
    ).first()
    if not blood_request:
        raise HTTPException(status_code=404, detail="Request not found.")

    # Only the assigned donor can cancel
    if blood_request.donor_id != donor.id:
        raise HTTPException(status_code=403, detail="You are not the assigned donor.")

    # Can only cancel if ACCEPTED — not if already FULFILLED
    if blood_request.status != RequestStatusEnum.ACCEPTED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel — request is {blood_request.status.value}.",
        )

    blood_request.donor_id = None
    blood_request.status = RequestStatusEnum.OPEN
    db.commit()
    db.refresh(blood_request)
    invalidate_cache("blood_requests:*")

    return build_request_response(blood_request)

@router.get("/matching", response_model=BloodRequestListResponse)
def get_matching_requests(
    radius_km:    float = Query(default=30.0),  
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(status_code=403, detail="Only registered donors can view matching requests.")

    compatible_recipient_groups = [
        group
        for group, compatible_donors in {
            "A+":  ["A+", "A-", "O+", "O-"],
            "A-":  ["A-", "O-"],
            "B+":  ["B+", "B-", "O+", "O-"],
            "B-":  ["B-", "O-"],
            "AB+": ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"],
            "AB-": ["AB-", "A-", "B-", "O-"],
            "O+":  ["O+", "O-"],
            "O-":  ["O-"],
        }.items()
        if donor.blood_group.value in compatible_donors
    ]

    requests_query = (
        db.query(BloodRequest)
        .options(joinedload(BloodRequest.hospital))
        .join(BloodRequest.hospital)
       .filter(
            BloodRequest.status == RequestStatusEnum.OPEN,
            BloodRequest.blood_group.in_(compatible_recipient_groups),
        )
    )

# ── GPS available — use DB bounding box first ──
    if donor.latitude is not None and donor.longitude is not None:
        lat_delta = radius_km / 111.0
    
        lng_delta = radius_km / (
            111.0 * math.cos(math.radians(donor.latitude))
        )

        requests_query = requests_query.filter(
            Hospital.latitude.between(
                donor.latitude - lat_delta,
                donor.latitude + lat_delta,
            ),
            Hospital.longitude.between(
                donor.longitude - lng_delta,
                donor.longitude + lng_delta,
            ),
        )

    requests = (
       requests_query
       .order_by(BloodRequest.created_at.desc())
       .all()
    )

    # ── Strategy 1: GPS available — filter by real distance ──
    if donor.latitude and donor.longitude:
        filtered = []
        for req in requests:
            hosp = req.hospital
            if hosp.latitude and hosp.longitude:
                dist = _haversine(
                    donor.latitude, donor.longitude,
                    hosp.latitude,  hosp.longitude,
                )
                if dist <= radius_km:
                    filtered.append(req)
            # Hospital with no coordinates — skip entirely
        requests = filtered

    # ── Strategy 2: No GPS — filter by donor's city only ──
    else:
        donor_city = donor.city.strip()

        requests_query = requests_query.filter(
            Hospital.city.ilike(donor_city)
        )

        requests = (
            requests_query
            .order_by(BloodRequest.created_at.desc())
            .all()
        )

    return {
        "total": len(requests),
        "items": [build_request_response(r) for r in requests],
    }


@router.post("/{request_id}/accept", response_model=BloodRequestResponse)
def accept_blood_request(
    request_id:       int,
    background_tasks: BackgroundTasks,
    current_user:     User    = Depends(get_current_user),
    db:               Session = Depends(get_db),
):
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(status_code=403, detail="Only registered donors can accept requests.")

    if donor.is_in_cooldown:
        raise HTTPException(
            status_code=403,
            detail=f"You cannot donate during your 90-day cooldown period. "
                   f"Eligible again on {donor.cooldown_until.isoformat()}.",
        )

    # WITH_FOR_UPDATE locks this row until transaction completes
    # If two donors hit this simultaneously, second one waits
    # then sees status=ACCEPTED and gets a 409
    blood_request = (
        db.query(BloodRequest)
        .filter(BloodRequest.id == request_id)
        .with_for_update()          # ← database row lock
        .first()
    )
    if not blood_request:
        raise HTTPException(status_code=404, detail="Blood request not found.")

    if blood_request.status != RequestStatusEnum.OPEN:
        raise HTTPException(
            status_code=409,
            detail=f"This request is already {blood_request.status.value}.",
        )

    compatible_donors = get_compatible_donor_groups(blood_request.blood_group.value)
    if donor.blood_group.value not in compatible_donors:
        raise HTTPException(
            status_code=400,
            detail=f"Your blood group {donor.blood_group.value} is not compatible.",
        )

    blood_request.donor_id = donor.id
    blood_request.status   = RequestStatusEnum.ACCEPTED
    db.commit()
    db.refresh(blood_request)
    invalidate_cache("blood_requests:*")

    background_tasks.add_task(
        notify_request_accepted,
        request_id=blood_request.id,
        hospital_id=blood_request.hospital_id,
        donor_name=current_user.full_name,
        blood_group=blood_request.blood_group.value,
    )

    return build_request_response(blood_request)

@router.patch("/{request_id}/fulfil", response_model=BloodRequestResponse)
def fulfil_blood_request(
    request_id: int,
    background_tasks: BackgroundTasks,
    current_hospital: Hospital = Depends(get_current_hospital),
    db: Session = Depends(get_db),
):
    blood_request = db.query(BloodRequest).filter(
        BloodRequest.id == request_id
    ).first()

    if not blood_request:
        raise HTTPException(404, "Blood request not found")

    if blood_request.hospital_id != current_hospital.id:
        raise HTTPException(403, "Not your request")

    if blood_request.status != RequestStatusEnum.ACCEPTED:
        raise HTTPException(
            409,
            "Only accepted requests can be fulfilled"
        )

    blood_request.status = RequestStatusEnum.FULFILLED
    db.commit()
    db.refresh(blood_request)

    invalidate_cache("blood_requests:*")

    background_tasks.add_task(
        notify_donation_fulfilled,
        request_id=blood_request.id,
        donor_id=blood_request.donor_id,
        hospital_id=current_hospital.id,
    )

    return build_request_response(blood_request)

# ── ADMIN ─────────────────────────────────────────────────────────────────────

@router.get("/admin/all", response_model=PagedResponse[BloodRequestResponse])
def admin_list_all_requests(
    pagination: PaginationParams = Depends(),
    status:      Optional[str] = Query(None),
    blood_group: Optional[str] = Query(None),
    city:        Optional[str] = Query(None),
    admin:       User          = Depends(require_admin),
    db:          Session       = Depends(get_db),
):
    """Admin sees all requests across all hospitals with pagination and filtering."""
    query = db.query(BloodRequest).join(BloodRequest.hospital)

    if status:
        query = query.filter(BloodRequest.status == status.lower())
    if blood_group:
        query = query.filter(BloodRequest.blood_group == blood_group)
    if city:
        from backend.models.hospitals import Hospital
        query = query.filter(Hospital.city.ilike(f"%{city}%"))

    total = query.count()

    items = (
        query
        .order_by(BloodRequest.created_at.desc())
        .offset(pagination.offset)
        .limit(pagination.page_size)
        .all()
    )

    return PagedResponse.create(
        items=[build_request_response(r) for r in items],
        total=total,
        params=pagination,                       # ← handles all pagination fields
    )


@router.patch("/admin/{request_id}/cancel", response_model=BloodRequestResponse)
def admin_cancel_request(
    request_id: int,
    admin:      User = Depends(require_admin),
    db:         Session = Depends(get_db),
):
    """Admin can cancel any request except fulfilled ones."""
    blood_request = db.query(BloodRequest).filter(BloodRequest.id == request_id).first()
    if not blood_request:
        raise HTTPException(status_code=404, detail="Blood request not found.")

    if blood_request.status == RequestStatusEnum.FULFILLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot cancel a fulfilled request.",
        )
    if blood_request.status == RequestStatusEnum.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Request is already cancelled.",
        )

    blood_request.status = RequestStatusEnum.CANCELLED
    db.commit()
    db.refresh(blood_request)
    return build_request_response(blood_request)



@router.get("/my-donations", response_model=PagedResponse[BloodRequestResponse])
def get_my_donations(
    page: int = 1,
    page_size: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns all blood requests where the current user
    is the assigned donor. This is the donor's personal history.
    """
    donor = db.query(Donor).filter(Donor.user_id == current_user.id).first()
    if not donor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only registered donors can view donation history.",
        )

    query = db.query(BloodRequest).filter(
        BloodRequest.donor_id == donor.id
    ).order_by(BloodRequest.created_at.desc())

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    total_pages = (total + page_size - 1) // page_size

    return {
        "items": [build_request_response(r) for r in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
    }



@router.get("/", response_model=PagedResponse[BloodRequestResponse])
# @limiter.limit("30/minute")
def list_blood_requests(
    request:    Request,
    pagination: PaginationParams = Depends(),
    filters:    RequestFilterParams = Depends(),
    db:         Session = Depends(get_db),
):
    cache_key = (
        f"blood_requests:"
        f"page={pagination.page}:"
        f"size={pagination.page_size}:"
        f"bg={filters.blood_group}:"
        f"city={filters.city}:"
        f"urgency={filters.urgency}:"
        f"status={filters.status}:"
        f"units_min={filters.units_needed_min}"
    )

    cached = get_cached(cache_key)
    if cached:
        return cached

    query = db.query(BloodRequest)
    if filters.blood_group:
        query = query.filter(BloodRequest.blood_group == filters.blood_group)

    if filters.city:
        query = query.join(Hospital, BloodRequest.hospital_id == Hospital.id)\
                     .filter(Hospital.city.ilike(f"%{filters.city}%"))

    if filters.urgency:
        query = query.filter(BloodRequest.urgency == filters.urgency)

    if filters.status:
        query = query.filter(BloodRequest.status == filters.status)

    if filters.units_needed_min is not None:
        query = query.filter(BloodRequest.units_needed >= filters.units_needed_min)

    total = query.count()
    blood_requests = query.order_by(BloodRequest.created_at.desc()).offset(pagination.offset).limit(pagination.page_size).all()

    result = PagedResponse.create(
        items=[build_request_response(r) for r in blood_requests],
        total=total,
        params=pagination
    )

    # Shorter TTL — blood requests are more time-sensitive
    set_cached(cache_key, result.model_dump(), ttl_seconds=30)

    return result