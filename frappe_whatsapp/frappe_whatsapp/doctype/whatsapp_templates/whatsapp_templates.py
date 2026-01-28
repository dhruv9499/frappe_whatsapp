"""Create whatsapp template."""

# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import os
import json
import re
import frappe
import magic
from frappe import _
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request, make_request
from frappe.desk.form.utils import get_pdf_link

from frappe_whatsapp.utils import get_whatsapp_account

class WhatsAppTemplates(Document):
    """Create whatsapp template."""

    def validate(self):
        self.set_whatsapp_account()
        if not self.language_code or self.has_value_changed("language"):
            lang_code = frappe.db.get_value("Language", self.language) or "en"
            self.language_code = lang_code.replace("-", "_")

        # Sanitize and validate template name
        if self.template_name:
            sanitized = self.sanitize_template_name(self.template_name)
            if sanitized != self.template_name.lower().replace(" ", "_"):
                # Auto-sanitize the actual_name that will be used
                if not self.actual_name or self.actual_name == self.template_name.lower().replace(" ", "_"):
                    self.actual_name = sanitized
            # Validate the sanitized name
            if not re.match(r'^[a-z][a-z0-9_]*$', sanitized):
                frappe.throw(
                    _("Template name '{0}' contains invalid characters. "
                      "Template names can only contain lowercase letters, numbers, and underscores, "
                      "and must start with a letter.").format(self.template_name),
                    title=_("Invalid Template Name")
                )

        if self.header_type in ["IMAGE", "DOCUMENT"] and self.sample:
            self.get_session_id()
            self.get_media_id()

        # Validate template body character limits
        # WhatsApp limits:
        # - Standard templates (no media header): 4096 characters
        # - Media templates (with IMAGE/VIDEO/DOCUMENT header): 1024 characters
        # - Authentication templates: 1024 characters
        # Note: During template approval, all templates are validated against 1024 char limit
        #       but actual sending allows up to 4096 for non-media templates
        if self.template:
            template_len = len(self.template)
            # Determine limit based on header type and category
            # Media templates (IMAGE, VIDEO, DOCUMENT) have stricter 1024 limit
            # Authentication templates also have 1024 limit
            # Standard templates (TEXT header or no header) allow up to 4096
            if self.category in ["AUTHENTICATION", "OTP"]:
                BODY_LIMIT = 1024
                category_desc = "AUTHENTICATION"
            elif self.header_type in ["IMAGE", "VIDEO", "DOCUMENT"]:
                BODY_LIMIT = 1024
                category_desc = "media (IMAGE/VIDEO/DOCUMENT header)"
            else:
                # Standard templates (TEXT header or no header) - MARKETING, UTILITY, etc.
                BODY_LIMIT = 4096
                category_desc = self.category or "standard"
            
            if template_len > BODY_LIMIT:
                frappe.throw(
                    _("Template body exceeds WhatsApp limit of {0} characters for {1} templates. Current length: {2}.").format(
                        BODY_LIMIT, category_desc, template_len
                    ),
                    title=_("Character Limit Exceeded")
                )

        # Validate header character limits
        if self.header_type == "TEXT" and self.header:
            header_len = len(self.header)
            HEADER_LIMIT = 60
            if header_len > HEADER_LIMIT:
                frappe.throw(
                    _("Header text exceeds WhatsApp limit of {0} characters. Current length: {1}.").format(
                        HEADER_LIMIT, header_len
                    ),
                    title=_("Character Limit Exceeded")
                )

        # Validate footer character limits
        if self.footer:
            footer_len = len(self.footer)
            FOOTER_LIMIT = 60
            if footer_len > FOOTER_LIMIT:
                frappe.throw(
                    _("Footer text exceeds WhatsApp limit of {0} characters. Current length: {1}.").format(
                        FOOTER_LIMIT, footer_len
                    ),
                    title=_("Character Limit Exceeded")
                )

        # Check if template has parameters and validate sample_values
        if self.template:
            param_count = self.get_parameter_count()
            if param_count > 0:
                if not self.sample_values:
                    frappe.throw(
                        _("Sample Values is required when template has parameters ({{1}}, {{2}}, etc.). "
                          "Please provide {0} sample values matching your {0} parameters. "
                          "Format: JSON array (e.g., [\"Value 1\", \"Value 2\"]), pipe-separated (Value 1|Value 2), or comma-separated (Value 1, Value 2). "
                          "Note: Use JSON or pipe format if values contain commas.").format(param_count),
                        title=_("Sample Values Required")
                    )
                else:
                    # Parse sample_values - try JSON first, then pipe, then comma
                    sample_list = self._parse_sample_values(self.sample_values, param_count)
                    if len(sample_list) != param_count:
                        frappe.throw(
                            _("Sample Values count ({0}) does not match template parameter count ({1}). "
                              "Please provide exactly {1} values. "
                              "Formats: JSON array [\"val1\", \"val2\"], pipe-separated (val1|val2), or comma-separated (val1, val2).").format(
                                len(sample_list), param_count
                            ),
                            title=_("Sample Values Mismatch")
                        )

        # Only update template if it's already been created (has an ID) and status allows updates
        # WhatsApp templates in PENDING/APPROVED status typically cannot be updated
        # Only update if status is not set or is in a state that allows updates
        if not self.is_new() and self.id:
            # Skip update if template is already submitted/approved (WhatsApp doesn't allow updates)
            if self.status and self.status.upper() in ["PENDING", "APPROVED", "REJECTED"]:
                # Don't attempt to update - WhatsApp doesn't allow modifying submitted templates
                pass
            else:
                try:
                    self.update_template()
                except Exception as e:
                    # Log the error but don't block saving if update fails
                    frappe.log_error(f"Failed to update WhatsApp template: {str(e)}", "WhatsApp Template Update")
                    # Don't raise - allow the document to save even if update fails

    def sanitize_template_name(self, name):
        """Sanitize template name to only contain lowercase letters, numbers, and underscores."""
        if not name:
            return ""
        # Convert to lowercase
        sanitized = name.lower()
        # Replace spaces, hyphens, and other common separators with underscores
        sanitized = re.sub(r'[\s\-\.]+', '_', sanitized)
        # Remove any characters that aren't lowercase letters, numbers, or underscores
        sanitized = re.sub(r'[^a-z0-9_]', '', sanitized)
        # Remove consecutive underscores
        sanitized = re.sub(r'_+', '_', sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')
        # Ensure it doesn't start with a number (WhatsApp requirement)
        if sanitized and sanitized[0].isdigit():
            sanitized = '_' + sanitized
        # Ensure it's not empty
        if not sanitized:
            sanitized = 'template_' + re.sub(r'[^a-z0-9]', '', name.lower())[:20]
        return sanitized

    def get_parameter_count(self):
        """Count the number of parameters in the template ({{1}}, {{2}}, etc.)."""
        if not self.template:
            return 0
        # Find all parameter placeholders like {{1}}, {{2}}, etc.
        matches = re.findall(r'\{\{(\d+)\}\}', self.template)
        if not matches:
            return 0
        # Return the highest parameter number found
        return max(int(m) for m in matches)

    def _parse_sample_values(self, sample_values_str, expected_count=None):
        """Parse sample_values string into a list.
        
        Supports multiple formats:
        1. JSON array: ["Value 1", "Value 2, with comma", "Value 3"]
        2. Pipe-separated: Value 1|Value 2, with comma|Value 3
        3. Comma-separated: Value 1, Value 2, Value 3 (breaks if values contain commas)
        
        Args:
            sample_values_str: The sample_values string
            expected_count: Optional expected count for validation
            
        Returns:
            List of sample value strings
        """
        if not sample_values_str or not sample_values_str.strip():
            return []
        
        sample_values_str = sample_values_str.strip()
        
        # Try JSON array format first (most robust)
        if sample_values_str.startswith("[") and sample_values_str.endswith("]"):
            try:
                parsed = json.loads(sample_values_str)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except (json.JSONDecodeError, ValueError):
                pass
        
        # Try pipe-separated format (good for values with commas)
        if "|" in sample_values_str:
            sample_list = [s.strip() for s in sample_values_str.split("|") if s.strip()]
            if expected_count is None or len(sample_list) == expected_count:
                return sample_list
        
        # Fall back to comma-separated (default, but breaks if values contain commas)
        sample_list = [s.strip() for s in sample_values_str.split(",") if s.strip()]
        return sample_list

    def _validate_sample_value_lengths(self, sample_list):
        """Validate that sample values don't exceed WhatsApp character limits.
        
        WhatsApp limits:
        - Body parameter: 32768 chars (if body-only template), ~1024 chars (if header/footer present)
        - Header parameter: 60 chars
        - Footer: 60 chars (no parameters allowed)
        
        For template creation, we validate against stricter limits to ensure approval.
        """
        # WhatsApp body parameter limit for templates (conservative limit for approval)
        BODY_PARAM_LIMIT = 1000  # Conservative limit for template approval
        HEADER_PARAM_LIMIT = 60
        
        for idx, value in enumerate(sample_list, start=1):
            value_len = len(value)
            
            # Check if this parameter is used in header
            header_has_param = self.header_type == "TEXT" and self.header and f"{{{{{idx}}}}}" in self.header
            
            if header_has_param:
                if value_len > HEADER_PARAM_LIMIT:
                    frappe.throw(
                        _("Sample value #{0} exceeds WhatsApp header parameter limit of {1} characters. "
                          "Current length: {2}. Value: '{3}'").format(
                            idx, HEADER_PARAM_LIMIT, value_len, value[:50] + "..." if value_len > 50 else value
                        ),
                        title=_("Character Limit Exceeded")
                    )
            else:
                # Body parameter
                if value_len > BODY_PARAM_LIMIT:
                    frappe.throw(
                        _("Sample value #{0} exceeds WhatsApp body parameter limit of {1} characters. "
                          "Current length: {2}. Value: '{3}'").format(
                            idx, BODY_PARAM_LIMIT, value_len, value[:50] + "..." if value_len > 50 else value
                        ),
                        title=_("Character Limit Exceeded")
                    )

    def set_whatsapp_account(self):
        """Set whatsapp account to default if missing"""
        if not self.whatsapp_account:
            default_whatsapp_account = get_whatsapp_account()
            if not default_whatsapp_account:
                frappe.throw(_("Please set a default outgoing WhatsApp Account or Select available WhatsApp Account"))
            else:
                self.whatsapp_account = default_whatsapp_account.name

    def get_session_id(self):
        """Upload media."""
        self.get_settings()
        file_path = self.get_absolute_path(self.sample)
        mime = magic.Magic(mime=True)
        file_type = mime.from_file(file_path)

        payload = {
            'file_length': os.path.getsize(file_path),
            'file_type': file_type,
            'messaging_product': 'whatsapp'
        }

        response = make_post_request(
            f"{self._url}/{self._version}/{self._app_id}/uploads",
            headers=self._headers,
            data=json.loads(json.dumps(payload))
        )
        self._session_id = response['id']

    def get_media_id(self):
        self.get_settings()

        headers = {
                "authorization": f"OAuth {self._token}"
            }
        file_name = self.get_absolute_path(self.sample)
        with open(file_name, mode='rb') as file: # b is important -> binary
            file_content = file.read()

        payload = file_content
        response = make_post_request(
            f"{self._url}/{self._version}/{self._session_id}",
            headers=headers,
            data=payload
        )

        self._media_id = response['h']

    def get_absolute_path(self, file_name):
        if(file_name.startswith('/files/')):
            file_path = f'{frappe.utils.get_bench_path()}/sites/{frappe.utils.get_site_base_path()[2:]}/public{file_name}'
        if(file_name.startswith('/private/')):
            file_path = f'{frappe.utils.get_bench_path()}/sites/{frappe.utils.get_site_base_path()[2:]}{file_name}'
        return file_path


    def after_insert(self):
        # Skip WhatsApp API call if flag is set (for fixtures/data import)
        if getattr(frappe.flags, 'skip_whatsapp_api', False):
            return
        
        # Skip if template already has an ID (imported from another system)
        if self.id:
            # Template was imported with existing WhatsApp ID, just sync status
            self._sync_existing_template()
            return

        if self.template_name:
            # Use sanitized name if not already set
            if not self.actual_name:
                self.actual_name = self.sanitize_template_name(self.template_name)
            else:
                # Ensure actual_name is also sanitized
                self.actual_name = self.sanitize_template_name(self.actual_name)

        self.get_settings()
        
        # Check if template already exists on WhatsApp before creating
        existing_template = self._check_template_exists_on_whatsapp()
        if existing_template:
            # Template exists on WhatsApp, sync it instead of creating
            self._sync_from_whatsapp_template(existing_template)
            return

        data = {
            "name": self.actual_name,
            "language": self.language_code,
            "category": self.category,
            "components": [],
        }

        # Normalize newlines: convert \r\n to \n, remove trailing newlines
        template_text = self.template.replace('\r\n', '\n').replace('\r', '\n') if self.template else ""
        template_text = template_text.rstrip('\n\r')
        
        body = {
            "type": "body",
            "text": template_text,
        }
        # WhatsApp API requires example field when template has parameters
        param_count = self.get_parameter_count()
        if param_count > 0:
            if self.sample_values:
                # Parse sample_values using smart parser (supports JSON, pipe, comma)
                sample_list = self._parse_sample_values(self.sample_values, param_count)
                # Validate count matches
                if len(sample_list) != param_count:
                    frappe.throw(
                        _("Sample Values count ({0}) does not match template parameter count ({1}). "
                          "Please provide exactly {1} values.").format(
                            len(sample_list), param_count
                        ),
                        title=_("Sample Values Mismatch")
                    )
                # Validate character limits for each sample value
                self._validate_sample_value_lengths(sample_list)
                body.update({"example": {"body_text": [sample_list]}})
            else:
                # Auto-generate sample values if missing (shouldn't happen due to validation)
                sample_list = [f"Sample {i}" for i in range(1, param_count + 1)]
                body.update({"example": {"body_text": [sample_list]}})

        data["components"].append(body)
        if self.header_type:
            data["components"].append(self.get_header())

        # add footer
        if self.footer:
            data["components"].append({"type": "footer", "text": self.footer})

        # add buttons
        if self.buttons:
            button_block = {"type": "buttons", "buttons": []}
            for btn in self.buttons:
                b = {"type": btn.button_type, "text": btn.button_label}

                if btn.button_type == "Visit Website":
                    b["type"] = "URL"
                    b["url"] = btn.website_url
                    if btn.url_type == "Dynamic" and btn.example_url:
                        b["example"] = btn.example_url.split(",")
                elif btn.button_type == "Call Phone":
                    b["type"] = "PHONE_NUMBER"
                    b["phone_number"] = btn.phone_number
                elif btn.button_type == "Quick Reply":
                    b["type"] = "QUICK_REPLY"

                button_block["buttons"].append(b)

            data["components"].append(button_block)

        try:
            response = make_post_request(
                f"{self._url}/{self._version}/{self._business_id}/message_templates",
                headers=self._headers,
                data=json.dumps(data),
            )
            self.id = response["id"]
            self.status = response["status"]
            self.db_update()
        except Exception as e:
            # Get full error response for debugging
            error_details = {}
            error_message = str(e)
            error_title = "Error"
            
            # Try to get error from integration_request flag (set by make_request before raise_for_status)
            if hasattr(frappe.flags, 'integration_request') and frappe.flags.integration_request:
                try:
                    error_response = frappe.flags.integration_request.json()
                    error_details = error_response.get("error", {})
                    error_message = error_details.get("error_user_msg") or error_details.get("message") or error_message
                    error_title = error_details.get("error_user_title", "Error")
                    
                    # Log full error for debugging
                    frappe.log_error(
                        f"WhatsApp Template Creation Error:\nRequest Data: {json.dumps(data, indent=2)}\n\nAPI Response: {json.dumps(error_response, indent=2)}",
                        "WhatsApp Template API Error"
                    )
                except Exception as parse_error:
                    # If JSON parsing fails, try to get text response
                    try:
                        error_text = frappe.flags.integration_request.text
                        frappe.log_error(
                            f"WhatsApp Template Creation Error (text response):\nRequest Data: {json.dumps(data, indent=2)}\n\nAPI Response Text: {error_text}\nParse Error: {str(parse_error)}",
                            "WhatsApp Template API Error"
                        )
                        error_message = f"{error_message}\n\nAPI Response: {error_text[:500]}"
                    except Exception:
                        frappe.log_error(
                            f"Error accessing API response: {str(parse_error)}\nOriginal error: {str(e)}\nRequest Data: {json.dumps(data, indent=2)}",
                            "WhatsApp Template API Error"
                        )
            else:
                # If integration_request is not available, log the exception
                frappe.log_error(
                    f"WhatsApp Template Creation Error (no integration_request): {str(e)}\nRequest Data: {json.dumps(data, indent=2)}",
                    "WhatsApp Template API Error"
                )
            
            frappe.throw(
                msg=error_message,
                title=error_title,
            )

    def _check_template_exists_on_whatsapp(self):
        """Check if template with same name and language already exists on WhatsApp."""
        try:
            response = make_request(
                "GET",
                f"{self._url}/{self._version}/{self._business_id}/message_templates?name={self.actual_name}",
                headers=self._headers,
            )
            
            for template in response.get("data", []):
                # Match by name and language
                if template.get("name") == self.actual_name and template.get("language") == self.language_code:
                    return template
            return None
        except Exception:
            # If check fails, proceed with creation attempt
            return None

    def _sync_from_whatsapp_template(self, template):
        """Sync local doc from existing WhatsApp template."""
        self.id = template.get("id")
        self.status = template.get("status", "PENDING")
        
        frappe.msgprint(
            _("Template '{0}' already exists on WhatsApp with status '{1}'. Synced local record.").format(
                self.actual_name, self.status
            ),
            alert=True,
            indicator="blue"
        )
        self.db_update()

    def _sync_existing_template(self):
        """Sync status for template that was imported with existing ID."""
        try:
            self.get_settings()
            response = make_request(
                "GET",
                f"{self._url}/{self._version}/{self._business_id}/message_templates?name={self.actual_name}",
                headers=self._headers,
            )
            
            for template in response.get("data", []):
                if template.get("id") == self.id or template.get("name") == self.actual_name:
                    self.status = template.get("status", self.status)
                    self.db_update()
                    frappe.msgprint(
                        _("Template synced from WhatsApp. Status: {0}").format(self.status),
                        alert=True,
                        indicator="blue"
                    )
                    return
            
            # Template not found on WhatsApp - might be deleted or different account
            frappe.msgprint(
                _("Template '{0}' not found on WhatsApp. It may have been deleted or belongs to a different account.").format(
                    self.actual_name
                ),
                alert=True,
                indicator="orange"
            )
        except Exception as e:
            frappe.log_error(f"Failed to sync template: {str(e)}", "WhatsApp Template Sync")

    def update_template(self):
        """Update template to meta."""
        self.get_settings()
        data = {"components": []}

        # Normalize newlines: convert \r\n to \n, remove trailing newlines
        template_text = self.template.replace('\r\n', '\n').replace('\r', '\n') if self.template else ""
        template_text = template_text.rstrip('\n\r')
        
        body = {
            "type": "body",
            "text": template_text,
        }
        # WhatsApp API requires example field when template has parameters
        param_count = self.get_parameter_count()
        if param_count > 0:
            if self.sample_values:
                # Parse sample_values using smart parser (supports JSON, pipe, comma)
                sample_list = self._parse_sample_values(self.sample_values, param_count)
                # Ensure we have exactly the right number of sample values
                if len(sample_list) < param_count:
                    # Pad with "Sample" if not enough values
                    sample_list.extend(["Sample"] * (param_count - len(sample_list)))
                elif len(sample_list) > param_count:
                    # Truncate if too many values
                    sample_list = sample_list[:param_count]
                body.update({"example": {"body_text": [sample_list]}})
            else:
                # Auto-generate sample values if missing (shouldn't happen due to validation)
                sample_list = [f"Sample {i}" for i in range(1, param_count + 1)]
                body.update({"example": {"body_text": [sample_list]}})
        data["components"].append(body)
        if self.header_type:
            data["components"].append(self.get_header())
        if self.footer:
            data["components"].append({"type": "footer", "text": self.footer})
        if self.buttons:
            button_block = {"type": "buttons", "buttons": []}
            for btn in self.buttons:
                b = {"type": btn.button_type, "text": btn.button_label}

                if btn.button_type == "Visit Website":
                    b["type"] = "URL"
                    b["url"] = btn.website_url
                    if btn.url_type == "Dynamic" and btn.example_url:
                        b["example"] = btn.example_url.split(",")
                elif btn.button_type == "Call Phone":
                    b["type"] = "PHONE_NUMBER"
                    b["phone_number"] = btn.phone_number
                elif btn.button_type == "Quick Reply":
                    b["type"] = "QUICK_REPLY"

                button_block["buttons"].append(b)

            data["components"].append(button_block)

        try:
            # Update template - WhatsApp API requires business_id in the URL
            # Note: WhatsApp typically doesn't allow updating templates once they're submitted
            # This will only work for templates that haven't been submitted yet
            make_post_request(
                f"{self._url}/{self._version}/{self._business_id}/{self.id}",
                headers=self._headers,
                data=json.dumps(data),
            )
        except Exception as e:
            # Extract error message from API response
            if hasattr(frappe.flags, 'integration_request') and frappe.flags.integration_request:
                try:
                    res = frappe.flags.integration_request.json().get("error", {})
                    error_message = res.get("error_user_msg", res.get("message", str(e)))
                    error_title = res.get("error_user_title", "Error")
                    
                    # If error indicates template can't be updated, provide helpful message
                    if "cannot be updated" in error_message.lower() or "not allowed" in error_message.lower():
                        frappe.throw(
                            _("WhatsApp templates cannot be updated once they are submitted (PENDING/APPROVED status). "
                              "You can only edit the template locally. To make changes, you may need to create a new template version."),
                            title=_("Template Update Not Allowed")
                        )
                    else:
                        frappe.throw(
                            msg=error_message,
                            title=error_title,
                        )
                except Exception:
                    # If we can't parse the error, check if it's a 400 error which might indicate update not allowed
                    if "400" in str(e) or "Bad Request" in str(e):
                        frappe.throw(
                            _("WhatsApp templates cannot be updated once they are submitted. "
                              "The template status is '{0}'. You can only edit the template locally. "
                              "To make changes, you may need to create a new template version.").format(self.status or "PENDING"),
                            title=_("Template Update Not Allowed")
                        )
                    else:
                        frappe.throw(
                            _("Failed to update WhatsApp template: {0}").format(str(e)),
                            title=_("Template Update Error")
                        )
            else:
                # If it's a 400 error, likely means update not allowed
                if "400" in str(e) or "Bad Request" in str(e):
                    frappe.throw(
                        _("WhatsApp templates cannot be updated once they are submitted. "
                          "The template status is '{0}'. You can only edit the template locally.").format(self.status or "PENDING"),
                        title=_("Template Update Not Allowed")
                    )
                else:
                    frappe.throw(
                        _("Failed to update WhatsApp template: {0}").format(str(e)),
                        title=_("Template Update Error")
                    )
            # res = frappe.flags.integration_request.json()['error']
            # frappe.throw(
            #     msg=res.get('error_user_msg', res.get("message")),
            #     title=res.get("error_user_title", "Error"),
            # )

    def get_settings(self):
        """Get whatsapp settings."""
        settings = frappe.get_doc("WhatsApp Account", self.whatsapp_account)
        self._token = settings.get_password("token")
        self._url = settings.url
        self._version = settings.version
        self._business_id = settings.business_id
        self._app_id = settings.app_id

        self._headers = {
            "authorization": f"Bearer {self._token}",
            "content-type": "application/json",
        }

    def on_trash(self):
        self.get_settings()
        url = f"{self._url}/{self._version}/{self._business_id}/message_templates?name={self.actual_name}"
        try:
            make_request("DELETE", url, headers=self._headers)
        except Exception:
            res = frappe.flags.integration_request.json().get("error", {})
            if res.get("error_user_title") == "Message Template Not Found":
                frappe.msgprint(
                    "Deleted locally", res.get("error_user_title", "Error"), alert=True
                )
            else:
                frappe.throw(
                    msg=res.get("error_user_msg"),
                    title=res.get("error_user_title", "Error"),
                )

    def get_header(self):
        """Get header format."""
        header = {"type": "header", "format": self.header_type}
        if self.header_type == "TEXT":
            header["text"] = self.header
            if self.sample:
                samples = self.sample.split(", ")
                header.update({"example": {"header_text": samples}})
        else:
            pdf_link = ''
            if not self.sample:
                key = frappe.get_doc(self.doctype, self.name).get_document_share_key()
                link = get_pdf_link(self.doctype, self.name)
                pdf_link = f"{frappe.utils.get_url()}{link}&key={key}"
            header.update({"example": {"header_handle": [self._media_id]}})

        return header

@frappe.whitelist()
def sync_template_status(template_name):
    """Sync status of a single template from WhatsApp API."""
    try:
        doc = frappe.get_doc("WhatsApp Templates", template_name)
        
        if not doc.id:
            frappe.throw(_("Template ID is missing. Cannot sync status."))
        
        if not doc.whatsapp_account:
            frappe.throw(_("WhatsApp Account is not set for this template."))
        
        # Get settings
        settings = frappe.get_doc("WhatsApp Account", doc.whatsapp_account)
        token = settings.get_password("token")
        url = settings.url
        version = settings.version
        business_id = settings.business_id
        
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json"
        }
        
        # Fetch template from WhatsApp API
        response = make_request(
            "GET",
            f"{url}/{version}/{business_id}/message_templates?name={doc.actual_name}",
            headers=headers,
        )
        
        # Find matching template
        template_found = None
        for template in response.get("data", []):
            if template.get("id") == doc.id or template.get("name") == doc.actual_name:
                template_found = template
                break
        
        if not template_found:
            frappe.throw(_("Template not found in WhatsApp API. It may have been deleted."))
        
        # Update status
        old_status = doc.status
        new_status = template_found.get("status", "PENDING")
        
        doc.status = new_status
        doc.save(ignore_permissions=True)
        
        frappe.db.commit()
        
        if old_status != new_status:
            return {
                "message": _("Template status updated from '{0}' to '{1}'").format(old_status, new_status),
                "old_status": old_status,
                "new_status": new_status
            }
        else:
            return {
                "message": _("Template status is already '{0}'").format(new_status),
                "old_status": old_status,
                "new_status": new_status
            }
            
    except Exception as e:
        frappe.log_error(f"Error syncing template status: {str(e)}", "WhatsApp Template Sync")
        frappe.throw(_("Failed to sync template status: {0}").format(str(e)))

@frappe.whitelist()
def fetch():
    """Fetch templates from meta."""
    """Later improve this code to pass a whatsapp account remove the js funcation so that it is called from whatsapp account doctype """
    whatsapp_accounts = frappe.get_all('WhatsApp Account', filters={'status': 'Active'}, fields=['name', 'token', 'url', 'version', 'business_id'])

    for account in whatsapp_accounts:
        # get credentials
        token = frappe.get_doc("WhatsApp Account", account.name).get_password("token")
        url = account.url
        version = account.version
        business_id = account.business_id

        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}

        try:
            response = make_request(
                "GET",
                f"{url}/{version}/{business_id}/message_templates",
                headers=headers,
            )

            for template in response["data"]:
                # Find existing template by actual_name or id
                existing_template = frappe.db.get_value(
                    "WhatsApp Templates",
                    filters={"actual_name": template["name"]},
                    fieldname="name"
                )
                
                if existing_template:
                    doc = frappe.get_doc("WhatsApp Templates", existing_template)
                else:
                    doc = frappe.new_doc("WhatsApp Templates")
                    doc.template_name = template["name"]
                    doc.actual_name = template["name"]

                # Update status and other fields
                old_status = doc.status
                doc.status = template["status"]
                doc.language_code = template["language"]
                doc.category = template["category"]
                doc.id = template["id"]
                doc.whatsapp_account = account.name

                # update components
                for component in template["components"]:

                    # update header
                    if component["type"] == "HEADER":
                        doc.header_type = component["format"]

                        # if format is text update sample text
                        if component["format"] == "TEXT":
                            doc.header = component["text"]
                    # Update footer text
                    elif component["type"] == "FOOTER":
                        doc.footer = component["text"]

                    # update template text
                    elif component["type"] == "BODY":
                        doc.template = component["text"]
                        if component.get("example"):
    			            # Check if 'body_text' exists before trying to access it
                            if component["example"].get("body_text"):
                                doc.sample_values = ",".join(
            	                    component["example"]["body_text"][0]
                    	        )

                    # Update buttons
                    elif component["type"] == "BUTTONS":
                        doc.set("buttons", [])
                        frappe.db.delete("WhatsApp Button", {"parent": doc.name, "parenttype": "WhatsApp Templates"})
                        typeMap = {
                            "URL": "Visit Website",
                            "PHONE_NUMBER": "Call Phone",
                            "QUICK_REPLY": "Quick Reply"
                        }

                        for i, button in enumerate(component.get("buttons", []), start=1):
                            btn = {}
                            btn["button_type"] = typeMap[button["type"]]
                            btn["button_label"] = button.get("text")
                            btn["sequence"] = i

                            if button["type"] == "URL":
                                btn["website_url"] = button.get("url")
                                if "{{" in btn["website_url"]:
                                    btn["url_type"] = "Dynamic"
                                else:
                                    btn["url_type"] = "Static"

                                if button.get("example"):
                                    btn["example_url"] = ",".join(button["example"])
                            elif button["type"] == "PHONE_NUMBER":
                                btn["phone_number"] = button.get("phone_number")

                            doc.append("buttons", btn)

                upsert_doc_without_hooks(doc, "WhatsApp Button", "buttons")

            return "Successfully fetched templates from meta"

        except Exception as e:
            # Check if frappe.flags.integration_request is set and has a .json() method
            if hasattr(frappe.flags.integration_request, 'json'):
                try:
                    res = frappe.flags.integration_request.json().get("error", {})
                    error_message = res.get("error_user_msg", res.get("message"))
                    frappe.throw(
                        msg=error_message,
                        title=res.get("error_user_title", "Error"),
                    )
                except (json.JSONDecodeError, KeyError):
                    # Handle cases where the response is not valid JSON or lacks the 'error' key
                    frappe.throw(f"An unexpected error occurred while fetching templates: {e}")
            else:
                # Handle cases where frappe.flags.integration_request doesn't exist or isn't a proper response object
                frappe.throw(f"An unexpected server error occurred: {e}")

def upsert_doc_without_hooks(doc, child_dt, child_field):
    """Insert or update a parent document and its children without hooks."""
    if frappe.db.exists(doc.doctype, doc.name):
        doc.db_update()
        frappe.db.delete(child_dt, {"parent": doc.name, "parenttype": doc.doctype})
    else:
        doc.db_insert()
    for d in doc.get(child_field):
        d.parent = doc.name
        d.parenttype = doc.doctype
        d.parentfield = child_field
        d.db_insert()
    frappe.db.commit()
