"""Workday selectors, keyed on `data-automation-id` wherever Workday exposes one.

Workday has shipped two DOM generations: older tenants put semantic ids directly in
`data-automation-id` (e.g. `legalNameSection_firstName`), newer ones wrap fields in
`data-automation-id="formField-<name>"` and give inputs ids like `name--legalName--firstName`.
Each logical field therefore lists candidates in priority order; the first visible match wins.

When a tenant breaks something, this is the only file that should need editing.
"""

from __future__ import annotations


def aid(value: str) -> str:
    return f'[data-automation-id="{value}"]'


def field(*names: str) -> str:
    """Candidates for a field inside a section: automation id, formField wrapper, or id suffix."""
    out: list[str] = []
    for n in names:
        out += [aid(n), f'{aid("formField-" + n)} input', f'input[id$="--{n}"]',
                f'button[id$="--{n}"]', f'textarea[id$="--{n}"]']
    return ", ".join(out)


# ---- job page / entry ----------------------------------------------------------------
APPLY_BUTTON = f'{aid("adventureButton")}, a{aid("adventureButton")}'
APPLY_MANUALLY = f'{aid("applyManually")}, a:has-text("Apply Manually"), button:has-text("Apply Manually")'
JOB_DESCRIPTION = f'{aid("jobPostingDescription")}, {aid("job-posting-details")}'
JOB_TITLE = f'{aid("jobPostingHeader")}, h2{aid("jobPostingHeader")}'

# ---- auth ----------------------------------------------------------------------------
SIGN_IN_LINK = f'{aid("signInLink")}, button:has-text("Sign In")'
CREATE_ACCOUNT_LINK = f'{aid("createAccountLink")}, button:has-text("Create Account")'
AUTH_EMAIL = f'{aid("email")}, input[type="email"]'
AUTH_PASSWORD = f'{aid("password")}, input[type="password"]:not({aid("verifyPassword")})'
AUTH_VERIFY_PASSWORD = aid("verifyPassword")
AUTH_TERMS_CHECKBOX = f'{aid("createAccountCheckbox")}, input[type="checkbox"][id*="terms" i]'
CREATE_ACCOUNT_SUBMIT = f'{aid("createAccountSubmitButton")}, {aid("click_filter")}[aria-label*="Create Account"]'
SIGN_IN_SUBMIT = f'{aid("signInSubmitButton")}, {aid("click_filter")}[aria-label*="Sign In"]'
AUTH_ERROR = f'{aid("errorMessage")}, [role="alert"]'

# ---- wizard chrome -------------------------------------------------------------------
PROGRESS_ACTIVE = f'{aid("progressBarActiveStep")}, [aria-current="step"]'
PAGE_HEADER = f'{aid("pageHeader")} h2, main h2'
NEXT_BUTTON = f'{aid("bottom-navigation-next-button")}, {aid("pageFooterNextButton")}'
BACK_BUTTON = f'{aid("bottom-navigation-back-button")}, {aid("pageFooterBackButton")}'
FIELD_ERRORS = f'{aid("errorMessage")}, {aid("inputAlert")}'
ERROR_BANNER = f'{aid("errorBanner")}, {aid("errorWidgetBarMessageCountCollapsed")}'
CONFIRMATION = (f'{aid("congratulationsPopup")}, {aid("applicationSubmitted")}, '
                ':text("Application Submitted"), :text("Thank you for applying")')

# ---- widgets -------------------------------------------------------------------------
LISTBOX = '[role="listbox"]'
LISTBOX_OPTION = f'[role="listbox"] [role="option"], {aid("promptOption")}'
PROMPT_OPTION = f'{aid("promptOption")}, [role="option"]'
SELECTED_PILL = f'{aid("selectedItem")}, {aid("selectedItemList")} [role="listitem"]'
FILE_INPUT = f'input[type="file"]{aid("file-upload-input-ref")}, input[type="file"]'
DELETE_ROW = f'{aid("panel-set-delete-button")}, button:has-text("Delete")'

# ---- My Information ------------------------------------------------------------------
MY_INFO = {
    "source": field("sourceDropdown", "sourcePrompt", "source--source", "source"),
    "previous_worker": f'{aid("previousWorker")}, {aid("formField-candidateIsPreviousWorker")}',
    "country": field("countryDropdown", "country--country", "country"),
    "first_name": field("legalNameSection_firstName", "legalName--firstName"),
    "last_name": field("legalNameSection_lastName", "legalName--lastName"),
    "address_line1": field("addressSection_addressLine1", "address--addressLine1"),
    "city": field("addressSection_city", "address--city"),
    "state": field("addressSection_countryRegion", "address--countryRegion"),
    "postal_code": field("addressSection_postalCode", "address--postalCode"),
    "phone_device_type": field("phone-device-type", "phoneNumber--phoneType"),
    "phone_country_code": field("country-phone-code", "phoneNumber--countryPhoneCode"),
    "phone_number": field("phone-number", "phoneNumber--phoneNumber"),
}

# ---- My Experience -------------------------------------------------------------------
WORK_SECTION = f'{aid("workExperienceSection")}, [role="group"][aria-labelledby*="Work-Experience"]'
EDU_SECTION = f'{aid("educationSection")}, [role="group"][aria-labelledby*="Education"]'
WEBSITE_SECTION = f'{aid("websiteSection")}, [role="group"][aria-labelledby*="Websites"]'
ADD_BUTTON = f'{aid("Add")}, {aid("Add Another")}, button:has-text("Add Another"), button:has-text("Add")'


def work_panel(i: int) -> str:
    """1-based work-experience panel."""
    return f'{aid(f"workExperience-{i}")}, [role="group"][aria-labelledby*="Work-Experience-{i}"]'


def edu_panel(i: int) -> str:
    return f'{aid(f"education-{i}")}, [role="group"][aria-labelledby*="Education-{i}"]'


WORK_FIELDS = {
    "title": field("jobTitle"),
    "company": field("company", "companyName"),
    "location": field("location"),
    "current": f'{aid("currentlyWorkHere")}, input[type="checkbox"][id$="--currentlyWorkHere"]',
    "start": f'{aid("formField-startDate")}, div[id$="--startDate"]',
    "end": f'{aid("formField-endDate")}, div[id$="--endDate"]',
    "description": field("description", "roleDescription"),
}
EDU_FIELDS = {
    "school": field("school", "schoolName"),
    "degree": field("degree"),
    "field_of_study": field("field-of-study", "fieldOfStudy"),
    "gpa": field("gpa", "gradeAverage"),
    "start": f'{aid("formField-firstYearAttended")}, div[id$="--firstYearAttended"]',
    "end": f'{aid("formField-lastYearAttended")}, div[id$="--lastYearAttended"]',
}
SKILLS_CONTAINER = f'{aid("formField-skills")}, {aid("skillsSection")}'
WEBSITE_INPUT = field("website", "url")
DATE_MONTH = f'{aid("dateSectionMonth-input")}, input[id$="dateSectionMonth-input"]'
DATE_DAY = f'{aid("dateSectionDay-input")}, input[id$="dateSectionDay-input"]'
DATE_YEAR = f'{aid("dateSectionYear-input")}, input[id$="dateSectionYear-input"]'
