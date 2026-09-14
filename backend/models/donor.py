from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SAEnum, ForeignKey, Date, Float, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from backend.database import Base
import enum

class BloodGroupEnum(str, enum.Enum):
    A_POS  = "A+"
    A_NEG  = "A-"
    B_POS  = "B+"
    B_NEG  = "B-"
    AB_POS = "AB+"
    AB_NEG = "AB-"
    O_POS  = "O+"
    O_NEG  = "O-"

class AvailabilityEnum(str, enum.Enum):
    AVAILABLE   = "available"
    UNAVAILABLE = "unavailable"
    BUSY        = "busy"

class Donor(Base):
    __tablename__ = "donors"

    __table_args__ = (
        Index("ix_donors_blood_group", "blood_group"),
        Index("ix_donors_city", "city"),
        Index("ix_donors_availability", "availability"),
        Index("ix_donors_is_active", "is_active"),
    )

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(String, ForeignKey("users.id"), unique=True, nullable=False)
    blood_group        = Column(SAEnum(BloodGroupEnum), nullable=False)
    city               = Column(String(100), nullable=False)
    age                = Column(Integer, nullable=False)
    availability       = Column(SAEnum(AvailabilityEnum), nullable=False, default=AvailabilityEnum.AVAILABLE)
    is_active          = Column(Boolean, default=True, nullable=False)
    last_donation_date = Column(Date, nullable=True)
    cooldown_until     = Column(Date, nullable=True)

    # Location — null until donor shares GPS
    latitude           = Column(Float, nullable=True)
    longitude          = Column(Float, nullable=True)

    created_at         = Column(DateTime(timezone=True), server_default=func.now())
    updated_at         = Column(DateTime(timezone=True), onupdate=func.now())
    user               = relationship("User", back_populates="donor_profile")
    donations          = relationship("BloodRequest", back_populates="donor")

    @property
    def is_in_cooldown(self) -> bool:
        from datetime import date
        if self.cooldown_until is None:
            return False
        return date.today() <= self.cooldown_until

    def __repr__(self):
        return f"<Donor {self.user_id} ({self.blood_group})>"