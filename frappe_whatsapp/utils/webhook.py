"""Webhook."""
import frappe
import json
import requests
import time
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()
	return post()


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	verify_token = frappe.form_dict.get("hub.verify_token")
	webhook_verify_token = frappe.db.get_value('WhatsApp Account', verify_token, 'webhook_verify_token')

	if not webhook_verify_token:
		frappe.throw("No matching WhatsApp account")

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def post():
	"""Post."""
	data = frappe.local.form_dict
	frappe.get_doc({
		"doctype": "WhatsApp Notification Log",
		"template": "Webhook",
		"meta_data": json.dumps(data)
	}).insert(ignore_permissions=True)

	messages = []
	phone_id = None
	try:
		messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
		phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get("phone_number_id")
	except KeyError:
		messages = data["entry"]["changes"][0]["value"].get("messages", [])
	sender_profile_name = next(
		(
			contact.get("profile", {}).get("name")
			for entry in data.get("entry", [])
			for change in entry.get("changes", [])
			for contact in change.get("value", {}).get("contacts", [])
		),
		None,
	)

	whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None
	if not whatsapp_account:
		return

	if messages:
		for message in messages:
			message_type = message['type']
			is_reply = True if message.get('context') and 'forwarded' not in message.get('context') else False
			reply_to_message_id = message['context']['id'] if is_reply else None
			if message_type == 'text':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['text']['body'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type":message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'reaction':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['reaction']['emoji'],
					"reply_to_message_id": message['reaction']['message_id'],
					"message_id": message['id'],
					"content_type": "reaction",
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'interactive':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['interactive']['nfm_reply']['response_json'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type": "flow",
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type in ["image", "audio", "video", "document"]:
				token = whatsapp_account.get_password("token")
				url = f"{whatsapp_account.url}/{whatsapp_account.version}/"

				media_id = message[message_type]["id"]
				headers = {
					'Authorization': 'Bearer ' + token

				}
				response = requests.get(f'{url}{media_id}/', headers=headers)

				if response.status_code == 200:
					media_data = response.json()
					media_url = media_data.get("url")
					mime_type = media_data.get("mime_type")
					file_extension = mime_type.split('/')[1]

					media_response = requests.get(media_url, headers=headers)
					if media_response.status_code == 200:

						file_data = media_response.content
						file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

						message_doc = frappe.get_doc({
							"doctype": "WhatsApp Message",
							"type": "Incoming",
							"from": message['from'],
							"message_id": message['id'],
							"reply_to_message_id": reply_to_message_id,
							"is_reply": is_reply,
							"message": message[message_type].get("caption",f"/files/{file_name}"),
							"content_type" : message_type,
							"profile_name":sender_profile_name,
							"whatsapp_account":whatsapp_account.name
						}).insert(ignore_permissions=True)

						file = frappe.get_doc(
							{
								"doctype": "File",
								"file_name": file_name,
								"attached_to_doctype": "WhatsApp Message",
								"attached_to_name": message_doc.name,
								"content": file_data,
								"attached_to_field": "attach"
							}
						).save(ignore_permissions=True)


						message_doc.attach = file.file_url
						message_doc.save()
			elif message_type == "button":
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['button']['text'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type": message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			else:
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message_id": message['id'],
					"message": message[message_type].get(message_type),
					"content_type" : message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

	else:
		changes = None
		try:
			changes = data["entry"][0]["changes"][0]
		except (KeyError, IndexError, TypeError):
			try:
				changes = data["entry"]["changes"][0]
			except (KeyError, IndexError, TypeError):
				frappe.log_error("Webhook structure error", f"Unable to parse webhook changes. Data: {json.dumps(data)}")
				return
		if changes:
			update_status(changes)
	return

def update_status(data):
	"""Update status hook."""
	if not data:
		return
		
	field = data.get("field")
	
	# Log all status updates for debugging
	frappe.logger().info(f"Webhook status update received - Field: {field}, Data: {json.dumps(data)}")
	
	if field == "message_template_status_update":
		value = data.get('value')
		if value:
			frappe.logger().info(f"Processing template status update: {json.dumps(value)}")
			update_template_status(value)
		else:
			frappe.log_error("Webhook template status error", f"Missing value in template status update. Data: {json.dumps(data)}")

	elif field == "messages":
		value = data.get('value')
		if value:
			update_message_status(value)
		else:
			frappe.log_error("Webhook message status error", f"Missing value in message status update. Data: {json.dumps(data)}")
	else:
		# Log unknown field types for debugging
		frappe.logger().debug(f"Unknown webhook field: {field}. Data: {json.dumps(data)}")

def update_template_status(data):
	"""
	Update template status based on Meta webhook.
	
	According to Meta documentation:
	https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/message_template_status_update
	
	Webhook structure:
	{
		"event": "APPROVED" | "REJECTED" | "PENDING" | "FLAGGED" | "DISABLED",
		"message_template_id": "123456789",
		"message_template_name": "template_name",
		"message_template_language": "en"
	}
	"""
	try:
		# Get event/status - Meta uses "event" field
		event = data.get("event") or data.get("status")
		
		# Get template ID - Meta uses "message_template_id" field
		message_template_id = data.get("message_template_id") or data.get("id")
		
		if not event:
			frappe.log_error("Template status update error", f"Missing event/status field. Data: {json.dumps(data)}")
			return
			
		if not message_template_id:
			frappe.log_error("Template status update error", f"Missing message_template_id field. Data: {json.dumps(data)}")
			return
		
		# Normalize status value (Meta sends uppercase like "APPROVED", "REJECTED")
		# but database might store in different case
		status = str(event).upper()
		
		# Check if template exists
		template_exists = frappe.db.exists("WhatsApp Templates", {"id": message_template_id})
		
		if not template_exists:
			# Try to find by actual_name as fallback
			template_name = data.get("message_template_name")
			if template_name:
				template_exists = frappe.db.exists("WhatsApp Templates", {"actual_name": template_name})
				if template_exists:
					template_doc = frappe.get_doc("WhatsApp Templates", {"actual_name": template_name})
					# Update the id if it was missing
					if not template_doc.id:
						template_doc.id = message_template_id
						template_doc.save(ignore_permissions=True)
					frappe.db.sql(
						"""UPDATE `tabWhatsApp Templates`
						SET status = %s
						WHERE actual_name = %s""",
						(status, template_name)
					)
					frappe.db.commit()
					frappe.logger().info(f"Updated template {template_name} (id: {message_template_id}) status to {status}")
					return
		
		if not template_exists:
			frappe.log_error("Template status update error", 
				f"Template not found. ID: {message_template_id}, Name: {data.get('message_template_name')}, Data: {json.dumps(data)}")
			return
		
		# Update template status by ID
		frappe.db.sql(
			"""UPDATE `tabWhatsApp Templates`
			SET status = %s
			WHERE id = %s""",
			(status, message_template_id)
		)
		frappe.db.commit()
		
		frappe.logger().info(f"Updated template {message_template_id} status to {status}")
	except Exception as e:
		frappe.log_error("Template status update error", 
			f"Error updating template status: {str(e)}\nData: {json.dumps(data)}\nTraceback: {frappe.get_traceback()}")

def update_message_status(data):
	"""Update message status."""
	id = data['statuses'][0]['id']
	status = data['statuses'][0]['status']
	conversation = data['statuses'][0].get('conversation', {}).get('id')
	name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

	doc = frappe.get_doc("WhatsApp Message", name)
	doc.status = status
	if conversation:
		doc.conversation_id = conversation
	doc.save(ignore_permissions=True)
