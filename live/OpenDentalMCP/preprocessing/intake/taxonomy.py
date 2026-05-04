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

ROUTE_SLIP = IntakeCategory(
    def_num=465,
    od_name="Route Slips",
    short_label="route_slip",
    description=(
        "Daily route slip / appointment routing form. Has patient name + "
        "date at the top, doctor's handwritten name/initials, list of "
        "procedures performed today (CompEx, ProphyAd, BWX, etc.), tooth "
        "chart annotations, and 'NV:' (next visit) field. Usually a "
        "single page, often pre-printed practice form. Title commonly "
        "'Routing Slip', 'Route Slip', or just 'Routing'."
    ),
)

TREATMENT_PLAN = IntakeCategory(
    def_num=133,
    od_name="Treatment Plans",
    short_label="treatment_plan",
    description=(
        "Printed treatment plan for patient signature. Lists procedures "
        "with tooth numbers, ADA D-codes (D1110, D2393, etc.), surfaces, "
        "fees, insurance estimates, and patient portion. Often labeled "
        "'Treatment Plan', 'Active Treatment Plan', or 'Estimate'. Usually "
        "ends with a patient signature line."
    ),
)

LAB_SLIP = IntakeCategory(
    def_num=330,
    od_name="Lab Slips",
    short_label="lab_slip",
    description=(
        "Lab work order or lab return slip. References a dental lab "
        "(crowns, dentures, retainers, night guards, etc.) by name + due "
        "date + shade + tooth number. Returned slips often include the "
        "case completion date and lab signature. Usually a half-page form."
    ),
)

BILLING_STATEMENT = IntakeCategory(
    def_num=454,
    od_name="Billing Statements",
    short_label="billing_statement",
    description=(
        "Patient billing statement / invoice. Shows account balance, aging, "
        "payment due, and a remit-to address. Often titled 'Statement' or "
        "'Invoice' with the practice's billing address at the top and the "
        "patient's mailing address as the addressee."
    ),
)

EOB = IntakeCategory(
    def_num=456,
    od_name="EOB's",
    short_label="eob",
    description=(
        "Explanation of Benefits printout from an insurance company "
        "(Delta Dental, MetLife, Cigna, etc.). Lists procedures with "
        "billed amount, allowed amount, deductible applied, insurance "
        "paid, and patient portion. Usually labeled 'Explanation of "
        "Benefits', 'EOB', 'Benefits Statement', or similar. Distinct "
        "from insurance ELIGIBILITY (which is a benefits-summary printout, "
        "not a per-claim payment record)."
    ),
)

CLAIM_ATTACHMENT = IntakeCategory(
    def_num=332,
    od_name="Claim Attachments",
    short_label="claim_attachment",
    description=(
        "Documentation submitted with an insurance claim — usually "
        "x-ray printouts, narrative letters, periodontal charting, or "
        "photos to justify a procedure. Often headed with the claim "
        "number and patient. Distinct from insurance eligibility, EOBs, "
        "and treatment plans."
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
    ROUTE_SLIP,
    TREATMENT_PLAN,
    LAB_SLIP,
    BILLING_STATEMENT,
    EOB,
    CLAIM_ATTACHMENT,
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
