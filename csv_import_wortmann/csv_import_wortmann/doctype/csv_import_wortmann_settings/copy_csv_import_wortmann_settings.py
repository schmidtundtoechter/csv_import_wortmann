# Copyright (c) 2025, ahmad mohammad and contributors
# For license information, please see license.txt
# File: csv_import_wortmann/csv_import_wortmann/csv_import_wortmann_settings/csv_import_wortmann_settings.py

import frappe
from frappe.model.document import Document
from frappe.utils import today, add_months, flt, cint
import csv
import io
from datetime import datetime
import traceback
import base64

class CSVImportWortmannSettings(Document):
    def before_save(self):
        """Validate settings before save"""
        pass

@frappe.whitelist()
def process_csv_import(doc_name, file_content, file_name):
    """Main function to process Wortmann CSV import"""
    try:
        settings_doc = frappe.get_doc("CSV Import Wortmann Settings", doc_name)
        
        # Handle file content - it might be base64 encoded or already a string
        if isinstance(file_content, str):
            try:
                # Try to decode as base64 first
                file_bytes = base64.b64decode(file_content)
                csv_text = file_bytes.decode('cp1252')
            except:
                # If base64 decode fails, assume it's already text
                csv_text = file_content
        else:
            # If it's bytes, decode directly
            csv_text = file_content.decode('cp1252')
        
        # Parse CSV content with semicolon delimiter
        csv_reader = csv.DictReader(io.StringIO(csv_text), delimiter=';')
        
        # Process data
        customer_data = {}
        total_licenses_before = 0
        errors = []
        
        # Handle special cases and group by customer
        rows = list(csv_reader)
        processed_rows = []
        skip_indices = set()
        
        for i, row in enumerate(rows):
            if i in skip_indices:
                continue
                
            try:
                # Convert German number format to float
                amount_str = row.get('Amount', '0').replace(',', '.')
                amount = flt(amount_str)
                total_licenses_before += abs(amount)
                
                # Check for special case (negative amount)
                if amount < 0:
                    # Find corresponding positive row
                    corresponding_row = find_corresponding_row(row, rows, i)
                    if corresponding_row:
                        corresponding_index = corresponding_row['index']
                        combined_row = combine_rows(row, corresponding_row['row'])
                        processed_rows.append(combined_row)
                        skip_indices.add(corresponding_index)
                    else:
                        errors.append(f"No corresponding positive row found for negative amount in line {i+2}")
                else:
                    # Check if this positive row has a corresponding negative row
                    corresponding_negative = find_corresponding_negative_row(row, rows, i)
                    if corresponding_negative and corresponding_negative['index'] not in skip_indices:
                        # This positive row will be combined with its negative counterpart
                        # Skip it here, it will be processed when we hit the negative row
                        pass
                    else:
                        # Standalone positive row
                        processed_rows.append(row)
                        
            except Exception as e:
                errors.append(f"Error processing row {i+2}: {str(e)}")
                continue
        
        # Group by customer
        for row in processed_rows:
            try:
                customer_nr = row.get('CustomCustomerNr', '').strip()
                if not customer_nr:
                    errors.append(f"Missing CustomCustomerNr in row")
                    continue
                    
                if customer_nr not in customer_data:
                    customer_data[customer_nr] = []
                customer_data[customer_nr].append(row)
            except Exception as e:
                errors.append(f"Error grouping row: {str(e)}")
                continue
        
        # Create invoices - RESILIENT APPROACH
        invoices_created = 0
        total_licenses_after = 0
        successful_customers = []
        
        for customer_nr, customer_rows in customer_data.items():
            try:
                # Validate customer exists first
                customer = frappe.get_all('Customer', 
                    filters={'custom_interne_kundennummer': customer_nr}, 
                    fields=['name', 'customer_name']
                )
                
                if not customer:
                    errors.append(f"Customer not found for internal number: {customer_nr}")
                    continue
                
                # Validate all items exist before creating invoice
                valid_rows = []
                for row in customer_rows:
                    article_nr = row.get('ArticleNumber_Mandant', '').strip()
                    if not article_nr:
                        continue
                    
                    # Find item by ArticleNumber_Mandant (external article number)
                    item = frappe.get_all('Item', 
                        filters={'custom_externe_artikelnummer': article_nr}, 
                        fields=['name', 'item_name', 'description']
                    )
                    
                    if not item:
                        errors.append(f"Item not found for external article number: {article_nr} (Customer: {customer_nr})")
                        continue
                    
                    # Check if quantity is valid
                    qty = convert_german_number(row.get('Amount', 0))
                    if qty <= 0:
                        errors.append(f"Invalid quantity {qty} for item {article_nr} (Customer: {customer_nr})")
                        continue
                    
                    valid_rows.append(row)
                
                # Only create invoice if we have valid rows
                if valid_rows:
                    invoice = create_wortmann_sales_invoice_safe(customer_nr, valid_rows, settings_doc, errors)
                    if invoice:
                        invoices_created += 1
                        successful_customers.append(customer_nr)
                        for item in invoice.items:
                            total_licenses_after += flt(item.qty)
                else:
                    errors.append(f"No valid items found for customer {customer_nr}")
                    
            except Exception as e:
                errors.append(f"Error processing customer {customer_nr}: {str(e)}")
                continue
        
        # Generate report
        report = generate_wortmann_report(total_licenses_before, total_licenses_after, invoices_created, errors, successful_customers)
        
        # Update history and results
        settings_doc.append('wortmann_importhistorie', {
            'importdatum': datetime.now(),
            'name_der_csv': file_name
        })
        
        settings_doc.append('wortmann_importergebnis', {
            'datum': datetime.now(),
            'name_der_csv': file_name,
            'importergebnis': report
        })
        
        settings_doc.save()
        
        return {
            'status': 'success',
            'message': f"Import completed. {invoices_created} invoices created successfully. {len(errors)} errors logged.",
            'invoices_created': invoices_created,
            'errors_count': len(errors),
            'report': report
        }
        
    except Exception as e:
        frappe.log_error(f"Wortmann CSV Import Error: {str(e)}\n{traceback.format_exc()}")
        return {
            'status': 'error',
            'message': f"Import failed: {str(e)}"
        }

def convert_german_number(number_str):
    """Convert German number format (135,4) to float (135.4)"""
    if not number_str:
        return 0.0
    try:
        return flt(str(number_str).replace(',', '.'))
    except:
        return 0.0

def find_corresponding_row(negative_row, all_rows, current_index):
    """Find the corresponding positive row for a negative amount row"""
    match_fields = ['CustomCustomerNr', 'ReferenceNumber', 'ArticleNumber_Mandant']
    
    # Check adjacent rows first (most common case)
    for offset in [-1, 1]:
        check_index = current_index + offset
        if 0 <= check_index < len(all_rows):
            check_row = all_rows[check_index]
            if all(negative_row.get(field) == check_row.get(field) for field in match_fields):
                amount = convert_german_number(check_row.get('Amount', 0))
                if amount > 0:  # Make sure it's positive
                    return {'row': check_row, 'index': check_index}
    
    # If not found adjacent, search all rows (rare case)
    for i, check_row in enumerate(all_rows):
        if i != current_index:
            if all(negative_row.get(field) == check_row.get(field) for field in match_fields):
                amount = convert_german_number(check_row.get('Amount', 0))
                if amount > 0:  # Make sure it's positive
                    return {'row': check_row, 'index': i}
    
    return None

def find_corresponding_negative_row(positive_row, all_rows, current_index):
    """Find the corresponding negative row for a positive amount row"""
    match_fields = ['CustomCustomerNr', 'ReferenceNumber', 'ArticleNumber_Mandant']
    
    # Check adjacent rows first
    for offset in [-1, 1]:
        check_index = current_index + offset
        if 0 <= check_index < len(all_rows):
            check_row = all_rows[check_index]
            if all(positive_row.get(field) == check_row.get(field) for field in match_fields):
                amount = convert_german_number(check_row.get('Amount', 0))
                if amount < 0:  # Make sure it's negative
                    return {'row': check_row, 'index': check_index}
    
    return None

def combine_rows(negative_row, positive_row):
    """Combine negative and positive rows"""
    combined = positive_row.copy()
    
    # Convert German numbers and add amounts
    pos_amount = convert_german_number(positive_row.get('Amount', 0))
    neg_amount = convert_german_number(negative_row.get('Amount', 0))
    final_amount = pos_amount + neg_amount
    combined['Amount'] = str(final_amount).replace('.', ',')
    
    # Convert and add total prices
    pos_total = convert_german_number(positive_row.get('TotalPrice', 0))
    neg_total = convert_german_number(negative_row.get('TotalPrice', 0))
    final_total = pos_total + neg_total
    combined['TotalPrice'] = str(final_total).replace('.', ',')
    
    # Price remains the same (should be identical in both rows)
    combined['Price'] = positive_row.get('Price', 0)
    
    return combined

def get_tax_account_rate(tax_account_name):
    """Fetch tax rate dynamically from Account DocType"""
    try:
        account = frappe.get_doc("Account", tax_account_name)
        # The tax rate might be stored in different fields depending on your setup
        # Common field names: tax_rate, rate, account_rate
        if hasattr(account, 'tax_rate') and account.tax_rate:
            return flt(account.tax_rate)
        elif hasattr(account, 'rate') and account.rate:
            return flt(account.rate)
        else:
            # If no rate found, default to 19%
            return 19.0
    except Exception as e:
        frappe.log_error(f"Error fetching tax rate for account {tax_account_name}: {str(e)}")
        return 19.0  # Default fallback

def create_wortmann_sales_invoice_safe(customer_nr, customer_rows, settings_doc, errors):
    """Create sales invoice for Wortmann customer - SAFE VERSION"""
    
    try:
        # Get customer (already validated to exist)
        customer = frappe.get_all('Customer', 
            filters={'custom_interne_kundennummer': customer_nr}, 
            fields=['name', 'customer_name']
        )[0]
        
        # Create sales invoice
        invoice = frappe.new_doc('Sales Invoice')
        invoice.customer = customer['name']
        invoice.posting_date = today()
        invoice.due_date = add_months(today(), 1)
        invoice.update_stock = 0
        
        # Get customer discount if available
        customer_discount_percentage = get_customer_discount(customer['customer_name'], settings_doc.wortmann_rabattwerte_je_kunde)
        
        # Add items to invoice
        items_added = 0
        for row in customer_rows:
            try:
                # Get item by ArticleNumber_Mandant (external article number)
                article_nr = row.get('ArticleNumber_Mandant', '').strip()
                item = frappe.get_all('Item', 
                    filters={'custom_externe_artikelnummer': article_nr}, 
                    fields=['name', 'item_name', 'description']
                )[0]
                
                # Convert German number format
                qty = convert_german_number(row.get('Amount', 0))
                rate = convert_german_number(row.get('Price', 0))
                amount = convert_german_number(row.get('TotalPrice', 0))
                
                if qty <= 0:
                    continue
                
                # Add item to invoice (without item-level discount)
                invoice.append('items', {
                    'item_code': item['name'],
                    'customer_item_code': article_nr,
                    'description': item.get('description') or item.get('item_name'),
                    'qty': qty,
                    'rate': rate,
                    'amount': amount  # Original amount, discount will be applied at invoice level
                })
                items_added += 1
                
            except Exception as e:
                errors.append(f"Error adding item {article_nr} to invoice for customer {customer_nr}: {str(e)}")
                continue
        
        if items_added == 0:
            return None  # No valid items added
        
        # Apply customer discount at invoice level
        if customer_discount_percentage > 0:
            invoice.additional_discount_percentage = customer_discount_percentage
        
        # Add taxes with dynamic rate from Account DocType
        try:
            tax_account = "1520 - Abziehbare Vorsteuer 19 % - AZ ITD - ÜJ"
            
            # Fetch tax rate dynamically from Account
            tax_rate = get_tax_account_rate(tax_account)
            
            invoice.append('taxes', {
                'charge_type': 'On Net Total',
                'account_head': tax_account,
                'rate': tax_rate,  # Dynamic rate from Account DocType
                'description': f'VAT {tax_rate}%'
            })
                
        except Exception as e:
            errors.append(f"Error adding tax to invoice for customer {customer_nr}: {str(e)}")
        
        # Calculate totals 
        try:
            invoice.run_method('calculate_taxes_and_totals')
        except Exception as e:
            errors.append(f"Error calculating totals for customer {customer_nr}: {str(e)}")
        
        # Check if invoice should be suppressed (zero amount)
        if settings_doc.nullrechnungen_unterdruecken and flt(invoice.grand_total) == 0:
            return None
        
        # Save invoice
        invoice.insert(ignore_permissions=True)
        
        return invoice
        
    except Exception as e:
        errors.append(f"Error creating invoice for customer {customer_nr}: {str(e)}")
        return None

def get_customer_discount(customer_name, discount_table):
    """Get customer discount percentage"""
    try:
        for row in discount_table:
            if row.kundenname and row.kundenname.strip() == customer_name.strip():
                return flt(row.rabatt_wert_in_prozent)
    except Exception as e:
        frappe.log_error(f"Error getting customer discount for {customer_name}: {str(e)}")
    return 0

def generate_wortmann_report(licenses_before, licenses_after, invoices_created, errors, successful_customers):
    """Generate import report"""
    report_lines = [
        f"Gesamtzahl Lizenzen vorher: {licenses_before}",
        f"Gesamtzahl Lizenzen nachher: {licenses_after}",
        f"Gesamtzahl erz. Rechnungen: {invoices_created}"
    ]
    
    if successful_customers:
        report_lines.append(f"Erfolgreiche Kunden: {', '.join(successful_customers)}")
    
    if errors:
        report_lines.append(f"\nFehler ({len(errors)}):")
        for error in errors:
            report_lines.append(f"- {error}")
    
    return "\n".join(report_lines)