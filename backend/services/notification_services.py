import logging
from datetime import datetime, date, timedelta
from backend.models.blood_requests import BloodRequest
from backend.models.donor import Donor, AvailabilityEnum
from backend.core.websocket_manager import manager
from backend.dependencies.__init__ import get_db
from backend.services.fcm_service import send_push_notification
from backend.models.user import User

logger = logging.getLogger(__name__)


async def _broadcast(room: str, event: dict):
    """Internal helper — broadcast to a room safely."""
    try:
        await manager.broadcast_to_room(room, event)
    except Exception as e:
        logger.error(f"[WS BROADCAST ERROR] room={room} error={e}")


async def notify_request_created(
    request_id: int,
    hospital_name: str,
    blood_group: str,
    urgency: str,
):
    db = next(get_db())
    try:
        logger.info(
            f"[REQUEST CREATED] ID={request_id} | "
            f"Hospital={hospital_name} | BG={blood_group}"
        )

        event = {
            "event": "request_created",
            "type": "REQUEST_CREATED",
            "payload": {
                "request_id": request_id,
                "hospital_name": hospital_name,
                "blood_group": blood_group,
                "urgency": urgency,
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

        await _broadcast("admin", event)
        await _broadcast("donors", event)

        # ── NEW: Send FCM push to all matching available donors ──
        from backend.models.donor import Donor, BloodGroupEnum, AvailabilityEnum
        from backend.utils.blood_compatibility import get_compatible_donor_groups

        compatible_donor_blood_groups = get_compatible_donor_groups(blood_group)

        # Find all active, available donors with matching blood group
        matching_donors = (
        db.query(User.fcm_token)
        .join(Donor, Donor.user_id == User.id)
        .filter(
            Donor.blood_group.in_(compatible_donor_blood_groups),
            Donor.availability == AvailabilityEnum.AVAILABLE,
            Donor.is_active == True,
            User.fcm_token != None,
        )
        .all()
    )

        logger.info(f"[FCM] Found {len(matching_donors)} donors to notify")

        urgency_emoji = {"critical": "URGENT", "high": "High Priority", "medium": "Medium", "low": "Low"}
        urgency_label = urgency_emoji.get(urgency.lower(), urgency)

        for (fcm_token,) in matching_donors:
            send_push_notification(
                fcm_token=fcm_token,
                title=f"Blood Needed — {blood_group}",
                body=f"{urgency_label}: {hospital_name} needs {blood_group} blood. Can you help?",
                data={
                    "request_id": str(request_id),
                    "blood_group": blood_group,
                    "urgency": urgency,
                    "type": "REQUEST_CREATED",
                },
            )

    except Exception as e:
        logger.error(f"[REQUEST CREATED ERROR] request_id={request_id} | error={e}")
    finally:
        db.close()




async def notify_request_accepted(
    request_id: int,
    hospital_id: int,
    donor_name: str,
    blood_group: str,
):
    """
    Background task — fires after a donor accepts a request.
    Broadcasts to:
      - hospital_{id} room → hospital sees donor assigned instantly
      - admin room         → admin sees status change
      - donors room        → donors see request status change
    """
    try:
        logger.info(
            f"[REQUEST ACCEPTED] "
            f"Request ID: {request_id} | "
            f"Donor: {donor_name} | "
            f"Timestamp: {datetime.utcnow().isoformat()}"
        )

        event = {
            "type": "REQUEST_ACCEPTED",
            "payload": {
                "request_id": request_id,
                "donor_name": donor_name,
                "blood_group": blood_group,
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

        await _broadcast(f"hospital_{hospital_id}", event)
        await _broadcast("admin", event)
        await _broadcast("donors", event)

    except Exception as e:
        logger.error(
            f"[REQUEST ACCEPTED ERROR] "
            f"request_id={request_id} | error={e}"
        )

async def notify_donation_fulfilled(
    request_id: int,
    donor_id: int,
    hospital_id: int,
):
    db = next(get_db())
    try:
        blood_request = db.query(BloodRequest).filter(BloodRequest.id == request_id).first()
        donor = db.query(Donor).filter(Donor.id == donor_id).first()

        if not blood_request or not donor:
            return

        # ── Set cooldown — 90 days from today ──
        today = date.today()
        donor.last_donation_date = today
        donor.cooldown_until     = today + timedelta(days=90)
        donor.availability       = AvailabilityEnum.UNAVAILABLE
        db.commit()

        logger.info(
            f"[COOLDOWN SET] Donor ID={donor_id} | "
            f"Unavailable until {donor.cooldown_until}"
        )

        # rest of your existing broadcast code unchanged...
        event = {
            "type": "REQUEST_FULFILLED",
            "payload": {
                "request_id": request_id,
                "blood_group": blood_request.blood_group.value,
                "hospital_name": blood_request.hospital.name,
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

        await _broadcast("admin", event)
        await _broadcast(f"hospital_{hospital_id}", event)

    except Exception as e:
        logger.error(f"[DONATION FULFILLED ERROR] request_id={request_id} | error={e}")
    finally:
        db.close()

async def _broadcast_availability_change(
    donor_id: int,
    full_name: str,
    availability: str,
):
    try:
        event = {
            "type": "DONOR_AVAILABILITY_CHANGED",
            "payload": {
                "donor_id": donor_id,
                "full_name": full_name,
                "availability": availability,
            },
        }

        await _broadcast("admin", event)

    except Exception as e:
        logger.error(
            f"[AVAILABILITY CHANGE ERROR] "
            f"donor_id={donor_id} | error={e}"
        )