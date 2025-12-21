import collections
import json
import re
from typing import List, Dict, Optional
from pydantic import BaseModel, validator, Field, root_validator
import frappe
from frappe import _
from erpnext_egypt_compliance.erpnext_eta.utils import (
    eta_datetime_issued_format,
    validate_allowed_values,
    eta_round,
)
from erpnext_egypt_compliance.erpnext_eta.ereceipt_schema import ItemWiseTaxDetails
from erpnext_egypt_compliance.erpnext_eta.legacy_einvoice import _abs_values

INVOICE_RAW_DATA = {}
COMPANY_DATA = {}

# ------------------- MODELS -------------------

class Signature(BaseModel):
    signatureType: str = Field(default="I")
    value: str = Field(...)


class TaxTotals(BaseModel):
    taxType: str
    amount: float = Field(default=0.0)

    @validator("amount")
    def apply_eta_round_tax_totals(cls, value, values):
        return eta_round(value)


class TaxableItem(BaseModel):
    taxType: str
    subType: str
    amount: float = Field(default=0.0)
    rate: float = Field(default=14)


class Discount(BaseModel):
    rate: float = Field(default=0.0)
    amount: float = Field(default=0.0)


class Value(BaseModel):
    currencySold: str = Field(...)
    amountEGP: float = Field(...)
    amountSold: float = Field(default=None)
    currencyExchangeRate: float = Field(default=None)

    @validator("amountEGP")
    def apply_eta_round_amount_egp(cls, value, values):
        return eta_round(value)


class InvoiceLine(BaseModel):
    description: str
    itemType: str
    itemCode: str
    internalCode: str = Field(default=None)
    unitType: str
    quantity: float
    salesTotal: float
    netTotal: float
    total: float
    discount: Optional[List[Discount]] = Field(default=None)
    taxableItems: List[TaxableItem]
    unitValue: Value
    valueDifference: float = Field(default=0.0)
    totalTaxableFees: float = Field(default=0.0)
    itemsDiscount: float = Field(default=0.0)

    @root_validator(pre=True)
    def validate_mandatories(cls, values):
        return validate_mandatory_fields(cls, values)

    @validator("itemType")
    def item_type_must_be_one_of(cls, value, values):
        allowed_types = ["GS1", "EGS"]
        return validate_allowed_values(value, allowed_types)

    @validator("salesTotal", "netTotal", "total")
    def apply_eta_round_sales_total(cls, value, values):
        return eta_round(value)

    @validator("taxableItems")
    def apply_eta_round_taxable_items(cls, value, values):
        for tax in value:
            tax.amount = eta_round(tax.amount)
        return value


class Delivery(BaseModel):
    approach: Optional[str] = Field(default=None)
    packaging: Optional[str] = Field(default=None)
    dateValidity: Optional[str] = Field(default=None)
    exportPort: Optional[str] = Field(default=None)
    countryOfOrigin: Optional[str] = Field(default=None)
    grossWeight: Optional[float] = Field(default=None)
    netWeight: Optional[float] = Field(default=None)
    terms: Optional[str] = Field(default=None)

    @classmethod
    def get_delivery_data(cls, invoice):
        from datetime import date, datetime
        from frappe.core.utils import html2text

        if not invoice.get("custom_eta_more_details", []):
            return cls()

        delivery = invoice.get("custom_eta_more_details")[0]
        
        date_validity = delivery.get("date_validity")
        if isinstance(date_validity, str):
            date_validity = datetime.strptime(date_validity, "%Y-%m-%d").strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(date_validity, date):
            date_validity = date_validity.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            date_validity = None

        if delivery.get("terms"):
            terms = frappe.get_value("Terms and Conditions", delivery.get("terms"), "terms", as_dict=True)
            if terms:
                delivery["terms"] = html2text(terms.get("terms"))

        return cls(
            approach=delivery.get("approach"),
            packaging=delivery.get("packaging"),
            dateValidity=date_validity,
            exportPort=delivery.get("export_port"),
            countryOfOrigin=delivery.get("country_of_origin"),
            grossWeight=delivery.get("gross_weight"),
            netWeight=delivery.get("net_weight"),
            terms=delivery.get("terms"),
        )


class Payment(BaseModel):
    bankName: Optional[str] = Field(default=None)
    bankAddress: Optional[str] = Field(default=None)
    bankAccountNo: Optional[str] = Field(default=None)
    bankAccountIBAN: Optional[str] = Field(default=None)
    swiftCode: Optional[str] = Field(default=None)
    terms: Optional[str] = Field(default=None)

    @validator("swiftCode", pre=True)
    def validate_swift_code(cls, v):
        if not v:
            return v
        v = v.strip().upper()
        pattern = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
        if not pattern.match(v):
            raise ValueError("Invalid SWIFT code format.")
        return v

    @classmethod
    def get_payment_data(cls, bank_account: str, terms: str | None = None):
        from frappe.utils import strip_html
        from frappe.contacts.doctype.address.address import get_address_display

        bank_account_data = frappe.get_value("Bank Account", bank_account, ["bank", "bank_account_no", "iban",], as_dict=True)
        swift_number = frappe.get_value("Bank", bank_account_data.get("bank"), "swift_number")

        if terms:
            terms = strip_html(terms)

        address_name = frappe.get_all(
            "Dynamic Link",
            filters={
                "link_doctype": "Bank",
                "link_name": bank_account_data.get("bank"),
                "parenttype": "Address",
                "parentfield": "links"
            },
            fields=["parent"],
            limit=1
        )

        bank_address = None
        if address_name:
            bank_address = frappe.get_doc("Address", address_name[0].parent)
            bank_address = get_address_display(bank_address.as_dict()).replace("<br>", " ")

        return cls(
            bankName=bank_account_data.bank,
            bankAddress=bank_address,
            bankAccountNo=bank_account_data.bank_account_no,
            bankAccountIBAN=bank_account_data.iban,
            swiftCode=swift_number,
            terms=terms,
        )


class ReceiverAddress(BaseModel):
    country: str
    governate: str
    regionCity: str
    street: str
    buildingNumber: str


class Receiver(BaseModel):
    type: str
    id: Optional[str] = None
    name: str = Field(...)
    address: ReceiverAddress = Field(...)

    @validator("type")
    def type_must_be_receiver(cls, value, values):
        allowed_types = ["B", "P", "F"]
        return validate_allowed_values(value, allowed_types)

    @validator("id", pre=True, always=True)
    def normalize_id(cls, value):
        if not value:
            return None
        return re.sub(r"[^A-Za-z0-9]", "", value)

    @validator("name")
    def name_default_values(cls, value, values):
        if values.get("type") == "P":
            return "Walkin Customer"
        return value


class IssuerAddress(BaseModel):
    branchId: str = Field(...)
    country: str = Field(default="EG")
    governate: str = Field(...)
    regionCity: str = Field(...)
    street: str = Field(...)
    buildingNumber: str = Field(...)
    postalCode: Optional[str] = None
    floor: Optional[str] = None
    room: Optional[str] = None
    landmark: Optional[str] = None
    additionalInformation: Optional[str] = None

    @root_validator(pre=True)
    def validate_mandatories(cls, values):
        return validate_mandatory_fields(cls, values)


class Issuer(BaseModel):
    id: str = Field(...)
    type: str = Field(default="B")
    name: str = Field(...)
    address: IssuerAddress = Field(...)

    @root_validator(pre=True)
    def validate_mandatories(cls, values):
        return validate_mandatory_fields(cls, values)

    @validator("type")
    def type_must_be_issuer(cls, value, values):
        allowed_types = ["B", "P", "F"]
        return validate_allowed_values(value, allowed_types)


# ------------------- FUNCTIONS (Receiver / Issuer / Invoice JSON) -------------------

def get_receiver():
    customer = frappe.get_doc("Customer", INVOICE_RAW_DATA.get("customer")).as_dict()
    customer_type = customer.get("eta_receiver_type", "P")
    customer_id = customer.get("tax_id", "").replace("-", "")

    # Default address
    address = ReceiverAddress(
        country="EG",
        governate="Egypt",
        regionCity="EG City",
        street="Street 1",
        buildingNumber="B0",
    )

    # If primary address exists
    customer_address_name = customer.get("customer_primary_address")
    if customer_address_name:
        customer_address = frappe.get_doc("Address", customer_address_name)
        address = ReceiverAddress(
            country=frappe.db.get_value("Country", customer_address.country, "code") or "EG",
            governate=customer_address.state or "NA",
            regionCity=customer_address.city or "NA",
            street=customer_address.address_line1 or "NA",
            buildingNumber=customer_address.building_number or "B0",
        )

    eta_receiver = Receiver(
        type=customer_type,
        id=customer_id,
        name=customer.get("customer_name"),
        address=address,
    )
    return eta_receiver


def validate_receiver_compliance(receiver: Receiver):
    if receiver.type == "B":
        if not receiver.id or not re.fullmatch(r"\d{9}", receiver.id):
            frappe.throw(_("Business customers must have a valid 9-digit Tax ID"), title=_("ETA Validation"))
    elif receiver.type == "P":
        if INVOICE_RAW_DATA.get("grand_total", 0) >= 25000:
            if not receiver.id or not re.fullmatch(r"\d{14}", receiver.id):
                frappe.throw(_("Individuals with invoices ≥ 25,000 EGP must have a valid 14-digit Tax ID"), title=_("ETA Validation"))
    return True


def validate_mandatory_fields(cls, values):
    required_fields = [name for name, field in cls.model_fields.items() if field.is_required()]
    error_fields = []
    for field_name, value in values.items():
        if isinstance(value, str):
            value = value.strip()
        if field_name in required_fields and not value:
            error_fields.append(f"Field '{field_name}' is required")
    if error_fields:
        error_fields = "<ul>" + "".join(f"<li>{error}</li>" for error in error_fields) + "</ul>"
        raise ValueError(error_fields)
    return values
