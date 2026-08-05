# Drop your payment batch here

Place the batch you want to validate in this folder, e.g.:

```
data/payment_batch.csv
```

Then from the project root run:

```bash
python run.py --batch data/payment_batch.csv
```

Notes:
- The file can be **CSV or JSON**.
- You can name it anything and place it anywhere - just pass the path to
  `--batch`. This folder is only a convention.
- Required columns: `payment_id`, `vendor_id`, `invoice_number`, and the
  payment amount (`payment_amount` or `amount`). See the main README for the
  full recommended column list and accepted aliases.
- The three source-of-truth files (vendor master, invoice register, payment
  history) live in `../reference_data/` - you do not put those here.
