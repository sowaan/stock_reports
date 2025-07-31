# Copyright (c) 2025, Sowaan and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, get_table_name, getdate
from frappe.utils.deprecations import deprecated
from pypika import functions as fn

from erpnext.stock.doctype.warehouse.warehouse import apply_warehouse_filter

SLE_COUNT_LIMIT = 100_000


def execute(filters=None):
	if not filters:
		filters = {}

	sle_count = frappe.db.estimate_count("Stock Ledger Entry")

	if (
		sle_count > SLE_COUNT_LIMIT
		and not filters.get("item_code")
		and not filters.get("warehouse")
		and not filters.get("warehouse_type")
	):
		frappe.throw(
			_("Please select either the Item or Warehouse or Warehouse Type filter to generate the report.")
		)

	if filters.from_date > filters.to_date:
		frappe.throw(_("From Date must be before To Date"))

	float_precision = cint(frappe.db.get_default("float_precision")) or 3

	meta = frappe.get_meta("Item")
	include_arabic_name = meta.has_field("custom_item_name_in_arabic")
	include_packing = meta.has_field("custom_packing_details")


	columns = get_columns(filters, include_arabic_name, include_packing)
	item_map = get_item_details(filters, include_arabic_name, include_packing)
	iwb_map = get_item_warehouse_batch_map(filters, float_precision)
	batch_date_map = get_batch_date_map(iwb_map)

	data = []
	for item in sorted(iwb_map):
		if not filters.get("item") or filters.get("item") == item:
			for wh in sorted(iwb_map[item]):
				for batch in sorted(iwb_map[item][wh]):
					qty_dict = iwb_map[item][wh][batch]
					# if qty_dict.opening_qty or qty_dict.in_qty or qty_dict.out_qty or qty_dict.bal_qty:
					if qty_dict.bal_qty:
						batch_doc = batch_date_map.get(batch, {})
						row = [
							item,
							wh,
							item_map[item]["item_group"],
							item_map[item]["item_name"]
						]
						if include_arabic_name:
							row.append(item_map[item].get("custom_item_name_in_arabic", ""))
						
						row.append(item_map[item].get("stock_uom", ""))

						if include_packing:
							row.append(item_map[item].get("custom_packing_details", ""))

						row += [
							flt(qty_dict.bal_qty, float_precision),
							batch,
							batch_doc.get("manufacturing_date", ""),
							batch_doc.get("expiry_date", ""),
							# flt(qty_dict.opening_qty, float_precision),
							# flt(qty_dict.in_qty, float_precision),
							# flt(qty_dict.out_qty, float_precision),
							flt(
								(qty_dict.bal_value / qty_dict.bal_qty) if qty_dict.bal_qty else 0,
								float_precision,
							),
							flt(qty_dict.bal_value, float_precision),
						]
						data.append(row)

	return columns, data



def get_columns(filters, include_arabic_name=False, include_packing=False):
	columns = [
		_("Item") + ":Link/Item:100",
		_("Warehouse") + ":Link/Warehouse:100",
		_("Item Group") + ":Link/Item Group:100",
		_("Item Name") + "::120",
	]

	if include_arabic_name:
		columns.append(_("Arabic Name") + "::120")

	columns.append(_("UOM") + "::80")

	if include_packing:
		columns.append(_("Packing") + "::105")

	columns += [
		_("Balance Qty") + ":Float:120",
		_("Batch") + ":Link/Batch:115",
		_("MFG Date") + ":Date:110",
		_("EXP Date") + ":Date:110",
		# _("Opening Qty") + ":Float:90",
		# _("In Qty") + ":Float:80",
		# _("Out Qty") + ":Float:80",
		_("Cost") + ":Float:120",
		_("Value") + ":Currency:120",
	]

	return columns



def get_stock_ledger_entries(filters):
	entries = get_stock_ledger_entries_for_batch_no(filters)

	entries += get_stock_ledger_entries_for_batch_bundle(filters)
	return entries


@deprecated
def get_stock_ledger_entries_for_batch_no(filters):
	if not filters.get("from_date"):
		frappe.throw(_("'From Date' is required"))
	if not filters.get("to_date"):
		frappe.throw(_("'To Date' is required"))

	posting_datetime = get_datetime(add_to_date(filters["to_date"], days=1))

	sle = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(sle)
		.select(
			sle.item_code,
			sle.warehouse,
			sle.batch_no,
			sle.posting_date,
			fn.Sum(sle.actual_qty).as_("actual_qty"),
			fn.Sum(sle.stock_value_difference).as_("stock_value_difference"),
		)
		.where(
			(sle.docstatus < 2)
			& (sle.is_cancelled == 0)
			& (sle.batch_no != "")
			& (sle.posting_datetime < posting_datetime)
		)
		.groupby(sle.voucher_no, sle.batch_no, sle.item_code, sle.warehouse)
	)

	query = apply_warehouse_filter(query, sle, filters)
	if filters.warehouse_type and not filters.warehouse:
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"warehouse_type": filters.warehouse_type, "is_group": 0},
			pluck="name",
		)

		if warehouses:
			query = query.where(sle.warehouse.isin(warehouses))

	for field in ["item_code", "batch_no", "company"]:
		if filters.get(field):
			query = query.where(sle[field] == filters.get(field))

	return query.run(as_dict=True) or []


def get_stock_ledger_entries_for_batch_bundle(filters):
	sle = frappe.qb.DocType("Stock Ledger Entry")
	batch_package = frappe.qb.DocType("Serial and Batch Entry")

	to_date = get_datetime(filters.to_date + " 23:59:59")

	query = (
		frappe.qb.from_(sle)
		.inner_join(batch_package)
		.on(batch_package.parent == sle.serial_and_batch_bundle)
		.select(
			sle.item_code,
			sle.warehouse,
			batch_package.batch_no,
			sle.posting_date,
			fn.Sum(batch_package.qty).as_("actual_qty"),
			fn.Sum(batch_package.stock_value_difference).as_("stock_value_difference"),
		)
		.where(
			(sle.docstatus < 2)
			& (sle.is_cancelled == 0)
			& (sle.has_batch_no == 1)
			& (sle.posting_datetime <= to_date)
		)
		.groupby(sle.voucher_no, batch_package.batch_no, batch_package.warehouse)
	)

	query = apply_warehouse_filter(query, sle, filters)
	if filters.warehouse_type and not filters.warehouse:
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"warehouse_type": filters.warehouse_type, "is_group": 0},
			pluck="name",
		)

		if warehouses:
			query = query.where(sle.warehouse.isin(warehouses))

	for field in ["item_code", "batch_no", "company"]:
		if filters.get(field):
			if field == "batch_no":
				query = query.where(batch_package[field] == filters.get(field))
			else:
				query = query.where(sle[field] == filters.get(field))

	return query.run(as_dict=True) or []


def get_item_warehouse_batch_map(filters, float_precision):
	sle = get_stock_ledger_entries(filters)
	iwb_map = {}

	from_date = getdate(filters["from_date"])
	to_date = getdate(filters["to_date"])

	for d in sle:
		iwb_map.setdefault(d.item_code, {}).setdefault(d.warehouse, {}).setdefault(
			d.batch_no,
			frappe._dict(
				{"opening_qty": 0.0, "in_qty": 0.0, "out_qty": 0.0, "bal_qty": 0.0, "bal_value": 0.0}
			),
		)
		qty_dict = iwb_map[d.item_code][d.warehouse][d.batch_no]
		if d.posting_date < from_date:
			qty_dict.opening_qty = flt(qty_dict.opening_qty, float_precision) + flt(
				d.actual_qty, float_precision
			)
		elif d.posting_date >= from_date and d.posting_date <= to_date:
			if flt(d.actual_qty) > 0:
				qty_dict.in_qty = flt(qty_dict.in_qty, float_precision) + flt(d.actual_qty, float_precision)
			else:
				qty_dict.out_qty = flt(qty_dict.out_qty, float_precision) + abs(
					flt(d.actual_qty, float_precision)
				)

		qty_dict.bal_qty = flt(qty_dict.bal_qty, float_precision) + flt(d.actual_qty, float_precision)
		qty_dict.bal_value += flt(d.stock_value_difference, float_precision)

	return iwb_map


# def get_item_details(filters):
# 	meta = frappe.get_meta("Item")
# 	custom_fields = []

# 	if meta.has_field("custom_item_name_in_arabic"):
# 		custom_fields.append("custom_item_name_in_arabic")
# 	if meta.has_field("custom_packing_details"):
# 		custom_fields.append("custom_packing_details")

# 	fields = ["name", "item_name", "description", "stock_uom", "item_group"] + custom_fields

# 	item_map = {}
# 	for d in frappe.qb.from_("Item").select(*fields).run(as_dict=1):
# 		item_map[d.name] = d

# 	return item_map

def get_item_details(filters, include_arabic_name=False, include_packing=False):
	fields = ["name", "item_name", "description", "stock_uom", "item_group"]

	if include_arabic_name:
		fields.append("custom_item_name_in_arabic")
	if include_packing:
		fields.append("custom_packing_details")

	item_map = {}
	for d in frappe.qb.from_("Item").select(*fields).run(as_dict=1):
		item_map[d.name] = d

	return item_map

def get_batch_date_map(iwb_map):
	batch_set = {
		batch
		for item_batches in iwb_map.values()
		for wh_batches in item_batches.values()
		for batch in wh_batches
	}

	if not batch_set:
		return {}

	Batch = frappe.qb.DocType("Batch")
	query = (
		frappe.qb.from_(Batch)
		.select(Batch.name, Batch.manufacturing_date, Batch.expiry_date)
		.where(Batch.name.isin(batch_set))
	)

	return {
		b["name"]: b
		for b in query.run(as_dict=True)
	}
