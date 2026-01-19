frappe.listview_settings['WhatsApp Templates'] = {

	onload: function(listview) {
		// Add primary action button for fetching templates
		listview.page.add_button(__("Fetch from WhatsApp"), function() {
			frappe.call({
				method: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch',
				freeze: true,
				freeze_message: __('Fetching templates from WhatsApp...'),
				callback: function(res) {
					if (res.message) {
						frappe.show_alert({
							message: res.message,
							indicator: 'green'
						}, 5);
					}
					listview.refresh();
				},
				error: function(r) {
					frappe.msgprint({
						message: __('Failed to fetch templates. Check error log for details.'),
						indicator: 'red',
						title: __('Fetch Failed')
					});
				}
			});
		}, 'primary');

		// Keep menu item as well for discoverability
		listview.page.add_menu_item(__("Fetch templates from WhatsApp"), function() {
			frappe.call({
				method: 'frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_templates.whatsapp_templates.fetch',
				freeze: true,
				freeze_message: __('Fetching templates from WhatsApp...'),
				callback: function(res) {
					if (res.message) {
						frappe.show_alert({
							message: res.message,
							indicator: 'green'
						}, 5);
					}
					listview.refresh();
				}
			});
		});
	}
};