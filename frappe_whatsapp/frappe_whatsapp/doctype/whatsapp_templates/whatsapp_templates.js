// Copyright (c) 2022, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on('WhatsApp Templates', {
	refresh: function(frm) {
		// Add sync status button if template has been created (has ID)
		if (frm.doc.id && frm.doc.whatsapp_account) {
			frm.add_custom_button(__('Sync Status from WhatsApp'), function() {
				frappe.call({
					method: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.sync_template_status',
					args: {
						template_name: frm.doc.name
					},
					freeze: true,
					freeze_message: __('Syncing template status from WhatsApp...'),
					callback: function(r) {
						if (r.message) {
							let msg = r.message.message || r.message;
							let indicator = r.message.old_status !== r.message.new_status ? 'green' : 'blue';
							frappe.show_alert({
								message: msg,
								indicator: indicator
							}, 5);
							frm.reload_doc();
						}
					},
					error: function(r) {
						frappe.msgprint({
							message: r.message || __('Failed to sync template status'),
							indicator: 'red',
							title: __('Sync Failed')
						});
					}
				});
			}, __('Actions'));
		}
	}
});
