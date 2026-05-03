"""
Curated DocCategory taxonomy for intake auto-filing.

The full list of OD DocCategory definitions is much larger (26+ entries
on this practice's installation, see preflight). For intake we restrict
the classifier to the subset of categories that actually receive end-of-day
scanned paper. Anything the classifier can't confidently place into one
of these falls into MISCELLANEOUS (137), which always queues for review.

These DefNums are specific to Huntington Beach Dental Center's OD instance
and were verified via preflight.run() on 2026-05-03.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntakeCategory:
    def_num: int
    od_name: str
    short_label: str
    description: str  # Used as part of the LLM classification prompt.


PATIENT_INFORMATION = IntakeCategory(
    def_num=138,
    od_name="Patient Information",
    short_label="patient_info",
    description=(
        "New-patient or update-info demographic form. Fields like Name, DOB, "
        "address, phone, email, employer, emergency contact, marital status. "
        "Sometimes titled 'Patient Information', 'New Patient Form', "
        "'Patient Registration'."
    ),
)

MEDICAL_HISTORY = IntakeCategory(
    def_num=461,
    od_name="Medical History",
    short_label="medical_history",
    description=(
        "Medical history questionnaire. Asks about medications, allergies, "
        "physician name, prior conditions, surgeries, pregnancies. Often a "
        "checklist of conditions like 'High blood pressure', 'Diabetes', "
        "'Heart problems'. Sometimes titled 'Medical History', "
        "'Health History', 'Patient Health Questionnaire'."
    ),
)

CORRESPONDENCE_CONSENTS = IntakeCategory(
    def_num=455,
    od_name="Correspondence/Consents",
    short_label="consent",
    description=(
        "Signed treatment consent forms. Includes Informed Consent for a "
        "specific procedure (extraction, anesthesia, root canal, implant, "
        "crown, etc.), HIPAA acknowledgment, financial agreement, "
        "general office policies. Has a signature line and patient signature."
    ),
)

HIPAA_PRIVACY = IntakeCategory(
    def_num=459,
    od_name="HIPAA Privacy/Forms",
    short_label="hipaa",
    description=(
        "HIPAA Notice of Privacy Practices acknowledgment specifically — "
        "the legally required privacy notice. Distinct from general consents. "
        "Often titled 'HIPAA', 'Notice of Privacy Practices', 'Privacy Notice'."
    ),
)

PATIENT_INSURANCE = IntakeCategory(
    def_num=462,
    od_name="Patient Insurance",
    short_label="insurance_card",
    description=(
        "Photo or scan of an insurance ID card (front or back). Shows "
        "insurance company name (Delta Dental, MetLife, Cigna, etc.), "
        "subscriber name, member/group ID, contact phone. Usually a small "
        "rectangular card occupying part of the page."
    ),
)

INSURANCE_ELIGIBILITY = IntakeCategory(
    def_num=460,
    od_name="Insurance Eligibility",
    short_label="insurance_eligibility",
    description=(
        "Insurance eligibility verification printout. Shows benefit details, "
        "annual maximum, deductible, coverage percentages, plan year, "
        "employer. Usually printed from an insurance portal."
    ),
)

REFERRALS = IntakeCategory(
    def_num=343,
    od_name="Referrals",
    short_label="referral",
    description=(
        "Inbound or outbound referral letter. Mentions another dentist or "
        "specialist (oral surgeon, periodontist, endodontist) and a clinical "
        "reason for referral. Often on letterhead from another practice."
    ),
)

MISCELLANEOUS = IntakeCategory(
    def_num=137,
    od_name="Miscellaneous",
    short_label="miscellaneous",
    description=(
        "Anything that doesn't clearly fit the categories above. Use this "
        "as a last resort when uncertain — it forces the document to be "
        "manually reviewed before filing."
    ),
)


ALL_CATEGORIES: tuple[IntakeCategory, ...] = (
    PATIENT_INFORMATION,
    MEDICAL_HISTORY,
    CORRESPONDENCE_CONSENTS,
    HIPAA_PRIVACY,
    PATIENT_INSURANCE,
    INSURANCE_ELIGIBILITY,
    REFERRALS,
    MISCELLANEOUS,
)


def by_short_label(label: str) -> IntakeCategory:
    """Return the IntakeCategory for a short_label like 'medical_history'.

    Falls back to MISCELLANEOUS if the label is unknown — never raises,
    so a wandering LLM can't break the pipeline.
    """
    for c in ALL_CATEGORIES:
        if c.short_label == label:
            return c
    return MISCELLANEOUS


def short_labels() -> tuple[str, ...]:
    """All short labels (for the LLM classification prompt's enum)."""
    return tuple(c.short_label for c in ALL_CATEGORIES)


def def_nums() -> set[int]:
    """The set of OD DocCategory DefNums this taxonomy covers."""
    return {c.def_num for c in ALL_CATEGORIES}
