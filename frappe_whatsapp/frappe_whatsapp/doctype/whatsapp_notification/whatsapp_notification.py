"""Notification."""

import json
import frappe

from frappe import _dict, _
from frappe.model.document import Document
from frappe.utils.safe_exec import get_safe_globals, safe_exec
from frappe.integrations.utils import make_post_request
from frappe.desk.form.utils import get_pdf_link
from frappe.utils import (
    add_to_date, nowdate, datetime,
    format_date, format_time, format_datetime,
    get_datetime, get_time, now_datetime, now
)

from frappe_whatsapp.utils import get_whatsapp_account


def sanitize_whatsapp_param(value):
    """
    Sanitize text for WhatsApp template parameters.
    WhatsApp API rejects: newlines, tabs, more than 4 consecutive spaces, empty strings.
    """
    import re
    if value in (None, ""):
        return "-"
    
    text = str(value)
    # Replace newlines and tabs with single space
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    # Collapse multiple spaces to max 4 consecutive spaces
    text = re.sub(r' {5,}', '    ', text)
    # Strip leading/trailing whitespace
    text = text.strip()
    
    if not text:
        return "-"
    return text


class WhatsAppNotification(Document):
    """Notification."""

    def get_value_from_path(self, doc, path):
        """Resolve dotted path like 'user.mobile_no' from doc."""
        if not path:
            return None
        
        parts = path.split(".")
        value = doc
        
        for part in parts:
            if value is None:
                return None
                
            if isinstance(value, Document):
                if hasattr(value, part):
                    # Try get() first (for regular fields)
                    link_value = value.get(part)
                    
                    # If get() returns None and it's not a field, try getattr() for properties
                    if link_value is None:
                        try:
                            meta = frappe.get_meta(value.doctype)
                            df = meta.get_field(part)
                            # If it's not a field in meta, it might be a property
                            if not df:
                                link_value = getattr(value, part, None)
                        except Exception:
                            # Fallback: try getattr if get() returned None
                            link_value = getattr(value, part, None)
                    
                    # Check if this is a Link field pointing to another doctype
                    try:
                        meta = frappe.get_meta(value.doctype)
                        df = meta.get_field(part)
                        if df and df.fieldtype == "Link" and link_value:
                            value = frappe.get_doc(df.options, link_value)
                        else:
                            value = link_value
                    except Exception:
                        value = link_value
                else:
                    return None
            elif isinstance(value, dict):
                value = value.get(part)
            else:
                return None
                
        return value

    def validate(self):
        """Validate."""
        if self.notification_type == "DocType Event" and self.field_name:
            # For dotted paths, only validate the first part exists as a field
            first_field = self.field_name.split(".")[0]
            fields = frappe.get_doc("DocType", self.reference_doctype).fields
            fields += frappe.get_all(
                "Custom Field",
                filters={"dt": self.reference_doctype},
                fields=["fieldname"]
            )
            if not any(field.fieldname == first_field for field in fields): # noqa
                frappe.throw(_("Field name {0} does not exist on DocType {1}").format(first_field, self.reference_doctype))
        if self.custom_attachment:
            if not self.attach and not self.attach_from_field:
                frappe.throw(_("Either {0} a file or add a {1} to send attachemt").format(
                    frappe.bold(_("Attach")),
                    frappe.bold(_("Attach from field")),
                ))

        if self.set_property_after_alert:
            meta = frappe.get_meta(self.reference_doctype)
            if not meta.get_field(self.set_property_after_alert):
                frappe.throw(_("Field {0} not found on DocType {1}").format(
                    self.set_property_after_alert,
                    self.reference_doctype,
                ))


    def send_scheduled_message(self) -> dict:
        """Specific to API endpoint Server Scripts."""
        safe_exec(
            self.condition, get_safe_globals(), dict(doc=self)
        )

        template = frappe.db.get_value(
            "WhatsApp Templates", self.template,
            fieldname='*'
        )

        if template and template.language_code:
            if self.get("_contact_list"):
                # send simple template without a doc to get field data.
                self.send_simple_template(template)
            elif self.get("_data_list"):
                # allow send a dynamic template using schedule event config
                # _doc_list shoud be [{"name": "xxx", "phone_no": "123"}]
                for data in self._data_list:
                    doc = frappe.get_doc(self.reference_doctype, data.get("name"))

                    self.send_template_message(doc, data.get("phone_no"), template, True)
        # return _globals.frappe.flags


    def send_simple_template(self, template):
        """ send simple template without a doc to get field data """
        for contact in self._contact_list:
            data = {
                "messaging_product": "whatsapp",
                "to": self.format_number(contact),
                "type": "template",
                "template": {
                    "name": template.actual_name,
                    "language": {
                        "code": template.language_code
                    },
                    "components": []
                }
            }
            self.content_type = template.get("header_type", "text").lower()
            self.notify(data, template_account=template.get("whatsapp_account"))


    def send_template_message(self, doc: Document, phone_no=None, default_template=None, ignore_condition=False):
        """Specific to Document Event triggered Server Scripts."""
        if self.disabled:
            return

        doc_data = doc.as_dict()
        if self.condition and not ignore_condition:
            # check if condition satisfies
            if not frappe.safe_eval(
                self.condition, get_safe_globals(), dict(doc=doc_data)
            ):
                return

        template = default_template or frappe.get_doc("WhatsApp Templates", self.template)

        if template:
            if self.field_name:
                phone_number = phone_no or self.get_value_from_path(doc, self.field_name)
                if not phone_number:
                    frappe.log_error(f"Could not resolve phone number from path: {self.field_name}", "WhatsApp Notification")
                    return
                phone_number = str(phone_number)
            else:
                phone_number = phone_no
                if phone_number:
                    phone_number = str(phone_number)

            data = {
                "messaging_product": "whatsapp",
                "to": self.format_number(phone_number),
                "type": "template",
                "template": {
                    "name": template.actual_name,
                    "language": {
                        "code": template.language_code
                    },
                    "components": []
                }
            }

            # Pass parameter values
            # If template_data_script is set, use it (overrides Fields table)
            if self.template_data_script:
                _locals = {"doc": doc, "frappe": frappe}
                try:
                    safe_exec(self.template_data_script, get_safe_globals(), _locals)
                    param_values = _locals.get("result", [])
                    if not isinstance(param_values, list):
                        frappe.throw(_("Template Data Script must set 'result' as a list of values"))
                    parameters = [{"type": "text", "text": sanitize_whatsapp_param(v)} for v in param_values]
                except Exception as e:
                    frappe.log_error(f"Error in template_data_script: {str(e)}", "WhatsApp Notification")
                    frappe.throw(_("Error in Template Data Script: {0}").format(str(e)))
            elif self.fields:
                parameters = []
                for field in self.fields:
                    try:
                        if getattr(field, "field_type", "Field") == "Expression":
                            # Evaluate Python expression
                            if not getattr(field, "expression", None):
                                frappe.throw(_("Expression is required when Field Type is 'Expression'"))
                            # Use safe_exec instead of safe_eval to allow frappe module access
                            # Note: RestrictedPython doesn't allow variable names starting with "_"
                            # Also, RestrictedPython blocks attribute access on modules, so we need to
                            # create a utils object that contains commonly used functions
                            # This allows expressions like frappe.utils.format_date(...) to work
                            # Create a simple object using type() for attribute access
                            UtilsObj = type('UtilsObj', (), {
                                "format_date": format_date,
                                "format_time": format_time,
                                "format_datetime": format_datetime,
                                "get_datetime": get_datetime,
                                "get_time": get_time,
                                "now_datetime": now_datetime,
                                "now": now,
                                "add_to_date": add_to_date,
                            })
                            utils_obj = UtilsObj()
                            
                            # Create frappe proxy object
                            FrappeObj = type('FrappeObj', (), {
                                "utils": utils_obj,
                                "db": frappe.db,
                                "get_doc": frappe.get_doc,
                                "get_value": frappe.get_value,
                                "get_list": frappe.get_list,
                                "session": frappe.session,
                            })
                            frappe_obj = FrappeObj()
                            
                            _locals = {
                                "doc": doc,
                                "frappe": frappe_obj,
                                "result": None,
                                # Also add functions directly for convenience
                                "format_date": format_date,
                                "format_time": format_time,
                                "format_datetime": format_datetime,
                                "get_datetime": get_datetime,
                                "get_time": get_time,
                                "now_datetime": now_datetime,
                                "now": now,
                            }
                            safe_exec(f"result = {field.expression}", get_safe_globals(), _locals)
                            value = _locals.get("result")
                        else:
                            # Use dotted path resolution
                            field_name = getattr(field, "field_name", None)
                            if not field_name:
                                frappe.throw(_("Field name is required when Field Type is 'Field'"))
                            value = self.get_value_from_path(doc, field_name)
                            
                            # Format dates/datetimes if needed
                            if isinstance(value, (datetime.date, datetime.datetime)):
                                if isinstance(doc, Document):
                                    value = doc.get_formatted(field_name)
                                else:
                                    value = str(value)
                            elif isinstance(doc, Document) and value is not None:
                                # Try to get formatted value for other field types
                                try:
                                    formatted_value = doc.get_formatted(field_name)
                                    if formatted_value:
                                        value = formatted_value
                                except Exception:
                                    pass
                        
                        parameters.append({
                            "type": "text",
                            "text": sanitize_whatsapp_param(value)
                        })
                    except Exception as e:
                        # Truncate error message to prevent CharacterLengthExceededError (max 140 chars for Title)
                        field_identifier = getattr(field, 'field_name', None) or getattr(field, 'expression', 'unknown')
                        # Truncate field_identifier if too long (expressions can be very long)
                        if len(field_identifier) > 30:
                            field_identifier = field_identifier[:27] + "..."
                        error_msg = str(e)
                        # Limit error message to ~80 chars to leave room for prefix (~60 chars including field_identifier)
                        if len(error_msg) > 80:
                            error_msg = error_msg[:77] + "..."
                        frappe.log_error(f"Error processing field {field_identifier}: {error_msg}", "WhatsApp Notification")
                        parameters.append({
                            "type": "text",
                            "text": "-"
                        })

            if parameters:
                data['template']["components"] = [{
                    "type": "body",
                    "parameters": parameters
                }]

            if self.attach_document_print:
                # frappe.db.begin()
                key = doc.get_document_share_key()  # noqa
                frappe.db.commit()
                print_format = "Standard"
                doctype = frappe.get_doc("DocType", doc_data['doctype'])
                if doctype.custom:
                    if doctype.default_print_format:
                        print_format = doctype.default_print_format
                else:
                    default_print_format = frappe.db.get_value(
                        "Property Setter",
                        filters={
                            "doc_type": doc_data['doctype'],
                            "property": "default_print_format"
                        },
                        fieldname="value"
                    )
                    print_format = default_print_format if default_print_format else print_format
                link = get_pdf_link(
                    doc_data['doctype'],
                    doc_data['name'],
                    print_format=print_format
                )

                filename = f'{doc_data["name"]}.pdf'
                url = f'{frappe.utils.get_url()}{link}&key={key}'

            elif self.custom_attachment:
                filename = self.file_name

                if self.attach_from_field:
                    file_url = doc_data[self.attach_from_field]
                    if not file_url.startswith("http"):
                        # get share key so that private files can be sent
                        key = doc.get_document_share_key()
                        file_url = f'{frappe.utils.get_url()}{file_url}&key={key}'
                else:
                    file_url = self.attach

                if file_url.startswith("http"):
                    url = f'{file_url}'
                else:
                    url = f'{frappe.utils.get_url()}{file_url}'

            if template.header_type == 'DOCUMENT':
                data['template']['components'].append({
                    "type": "header",
                    "parameters": [{
                        "type": "document",
                        "document": {
                            "link": url,
                            "filename": filename
                        }
                    }]
                })
            elif template.header_type == 'IMAGE':
                data['template']['components'].append({
                    "type": "header",
                    "parameters": [{
                        "type": "image",
                        "image": {
                            "link": url
                        }
                    }]
                })
            self.content_type = template.header_type.lower()

            if template.buttons:
                button_fields = self.button_fields.split(",") if self.button_fields else []
                for idx, btn in enumerate(template.buttons):
                    if btn.button_type == "Visit Website" and btn.url_type == "Dynamic":
                        if button_fields:
                            data['template']['components'].append({
                                "type": "button",
                                "sub_type": "url",
                                "index": str(idx),
                                "parameters": [
                                    {"type": "text", "text": doc.get(button_fields.pop(0))}
                                ]
                            })


            self.notify(data, doc_data, template_account=template.whatsapp_account)

    def notify(self, data, doc_data=None, template_account=None):
        """Notify."""
        # Use template's whatsapp account if available, otherwise use default outgoing account
        if template_account:
            whatsapp_account = frappe.get_doc("WhatsApp Account", template_account)
        else:
            whatsapp_account = get_whatsapp_account(account_type='outgoing')

        if not whatsapp_account:
            frappe.throw(_("Please set a default outgoing WhatsApp Account"))

        token = whatsapp_account.get_password("token")

        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json"
        }
        try:
            success = False
            response = make_post_request(
                f"{whatsapp_account.url}/{whatsapp_account.version}/{whatsapp_account.phone_id}/messages",
                headers=headers, data=json.dumps(data)
            )

            if not self.get("content_type"):
                self.content_type = 'text'

            parameters = None
            if data["template"]["components"]:
                parameters = [param["text"] for param in data["template"]["components"][0]["parameters"]]
                parameters = frappe.json.dumps(parameters, default=str)

            new_doc = {
                "doctype": "WhatsApp Message",
                "type": "Outgoing",
                "message": str(data['template']),
                "to": data['to'],
                "message_type": "Template",
                "message_id": response['messages'][0]['id'],
                "content_type": self.content_type,
                "use_template": 1,
                "template": self.template,
                "template_parameters": parameters,
                "whatsapp_account": whatsapp_account.name,
            }

            if doc_data:
                new_doc.update({
                    "reference_doctype": doc_data.doctype,
                    "reference_name": doc_data.name,
                })

            frappe.get_doc(new_doc).save(ignore_permissions=True)

            if doc_data and self.set_property_after_alert and self.property_value:
                if doc_data.doctype and doc_data.name:
                    fieldname = self.set_property_after_alert
                    value = self.property_value
                    meta = frappe.get_meta(doc_data.get("doctype"))
                    df = meta.get_field(fieldname)
                    if df:
                        if df.fieldtype in frappe.model.numeric_fieldtypes:
                            value = frappe.utils.cint(value)

                        frappe.db.set_value(doc_data.get("doctype"), doc_data.get("name"), fieldname, value)

            frappe.msgprint("WhatsApp Message Triggered", indicator="green", alert=True)
            success = True

        except Exception as e:
            error_message = str(e)
            if frappe.flags.integration_request:
                response = frappe.flags.integration_request.json().get('error', {})
                if response:
                    error_message = response.get('Error', response.get("message"))

            frappe.msgprint(
                f"Failed to trigger whatsapp message: {error_message}",
                indicator="red",
                alert=True
            )
        finally:
            if not success:
                meta = {"error": error_message}
            else:
                meta = frappe.flags.integration_request.json()
            frappe.get_doc({
                "doctype": "WhatsApp Notification Log",
                "template": self.template,
                "meta_data": meta
            }).insert(ignore_permissions=True)


    def on_trash(self):
        """On delete remove from schedule."""
        frappe.cache().delete_value("whatsapp_notification_map")


    def format_number(self, number):
        """Format number."""
        if (number.startswith("+")):
            number = number[1:len(number)]

        return number

    def get_documents_for_today(self):
        """get list of documents that will be triggered today"""
        docs = []

        diff_days = self.days_in_advance
        if self.doctype_event == "Days After":
            diff_days = -diff_days

        reference_date = add_to_date(nowdate(), days=diff_days)
        reference_date_start = reference_date + " 00:00:00.000000"
        reference_date_end = reference_date + " 23:59:59.000000"

        doc_list = frappe.get_all(
            self.reference_doctype,
            fields="name",
            filters=[
                {self.date_changed: (">=", reference_date_start)},
                {self.date_changed: ("<=", reference_date_end)},
            ],
        )

        for d in doc_list:
            doc = frappe.get_doc(self.reference_doctype, d.name)
            self.send_template_message(doc)
            # print(doc.name)


@frappe.whitelist()
def call_trigger_notifications():
    """Trigger notifications."""
    try:
        # Directly call the trigger_notifications function
        trigger_notifications()  
    except Exception as e:
        # Log the error but do not show any popup or alert
        frappe.log_error(frappe.get_traceback(), "Error in call_trigger_notifications")
        # Optionally, you could raise the exception to be handled elsewhere if needed
        raise e

def trigger_notifications(method="daily"):
    if frappe.flags.in_import or frappe.flags.in_patch:
        # don't send notifications while syncing or patching
        return

    if method == "daily":
        doc_list = frappe.get_all(
            "WhatsApp Notification", filters={"doctype_event": ("in", ("Days Before", "Days After")), "disabled": 0}
        )
        for d in doc_list:
            alert = frappe.get_doc("WhatsApp Notification", d.name)
            alert.get_documents_for_today()
           
