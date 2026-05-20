# Etsy Inventory System — TODO

## Printer Integration
- [ ] Connect to 3D printer API (OrcaSlicer/Klipper/Bambu) for live filament tracking
- [ ] Auto-deduct filament when a print job completes (webhook or polling)
- [ ] Track print failures and wasted filament separately
- [ ] Per-listing filament usage (currently hardcoded to 84g — different parts use different amounts)

## Inventory
- [ ] Per-listing material overrides (some items may use more/less filament)
- [ ] Support for multi-part orders (bundles that use multiple prints)
- [ ] Track filament color/brand separately from type
- [ ] Reorder alerts with suggested order quantities (based on sales velocity)

## Orders & Revenue
- [ ] Revenue report: last 30 / 90 days, by listing
- [ ] Profit margin report (revenue minus COGS)
- [ ] Etsy listing stats integration (views → conversion rate → projected revenue)
- [ ] Export orders to CSV for accounting

## Business Management
- [ ] Variable filament roll cost per purchase (currently $11 default)
- [ ] Track consumable costs (bungee cords, envelopes, labels)
- [ ] Sales tax tracking by state
- [ ] Etsy fee tracking (transaction %, listing fee, payment processing)

## Automation
- [ ] Scheduled sync (cron/systemd timer) instead of manual `python cli.py sync`
- [ ] Email/SMS low stock alerts
- [ ] Auto-restock Etsy listing quantities after printing a batch
