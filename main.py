#!/usr/bin/env python3
"""
Handelsregister Scraper - CLI Entry Point

A tool for finding AI and robotics startups in the German Handelsregister
using free official data sources.

Usage:
    python main.py bulk-load      # Load data from OffeneRegister.de
    python main.py scan           # Scan bundesAPI for new companies
    python main.py report         # Generate summary report
    python main.py export         # Export data to CSV/JSON
    python main.py stats          # Show database statistics
"""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from persistence.database import Database
from sources.offeneregister import OffeneRegisterSource
from sources.bundesapi import BundesAPISource, create_daily_scan_job
from processing.filters import AIRoboticsFilter, DEFAULT_AI_KEYWORDS
from processing.startup_scorer import StartupScorer
from export.exporters import CSVExporter, JSONExporter, ReportGenerator

console = Console()


@click.group()
@click.option('--db', default='handelsregister.db', help='Database file path')
@click.pass_context
def cli(ctx, db):
    """Handelsregister Scraper - Find AI/Robotics startups in Germany."""
    ctx.ensure_object(dict)
    ctx.obj['db_path'] = db


@cli.command()
@click.option('--limit', default=None, type=int, help='Limit records to process (for testing)')
@click.option('--min-score', default=1, help='Minimum AI relevance score')
@click.option('--force-download', is_flag=True, help='Force re-download of bulk data')
@click.pass_context
def bulk_load(ctx, limit, min_score, force_download):
    """
    Load companies from OffeneRegister.de bulk data.

    This downloads ~260MB of data and filters for AI/robotics companies.
    Run this once to populate the database with historical data.
    """
    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Handelsregister Bulk Loader[/bold blue]")
    console.print("=" * 50)

    # Initialize components
    db = Database(db_path)
    source = OffeneRegisterSource()
    filter_ = AIRoboticsFilter()

    # Check for existing data
    existing_count = db.count_companies(source='offeneregister')
    if existing_count > 0 and not force_download:
        console.print(f"\n[yellow]Warning:[/yellow] Database already has {existing_count:,} companies from OffeneRegister.")
        if not click.confirm("Continue and add new companies?"):
            return

    # Download if needed
    console.print("\n[bold]Step 1: Downloading bulk data...[/bold]")
    file_info = source.get_file_info()

    if file_info['exists'] and not force_download:
        console.print(f"Using cached file: {file_info['path']}")
        console.print(f"Size: {file_info['size_mb']:.1f} MB")
    else:
        source.download(force=force_download)

    # Create filter function
    def filter_func(record):
        return filter_.quick_filter(record.name)

    # Load with progress
    console.print("\n[bold]Step 2: Loading and filtering companies...[/bold]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:,} records"),
        console=console,
    ) as progress:
        task = progress.add_task("Processing...", total=None)

        def progress_callback(count):
            progress.update(task, completed=count)

        stats = source.load_to_database(
            db=db,
            filter_func=filter_func,
            batch_size=1000,
            limit=limit,
            progress_callback=progress_callback,
        )

    # Update AI scores and startup scores for loaded companies
    console.print("\n[bold]Step 3: Calculating AI relevance scores...[/bold]")

    # Get companies without scores
    companies = db.search_companies(source='offeneregister', limit=100000)
    updated_ai = 0

    with Progress(console=console) as progress:
        task = progress.add_task("Scoring AI relevance...", total=len(companies))

        for company in companies:
            result = filter_.filter_company(
                name=company['name'],
                purpose=company.get('purpose'),
            )

            if result.relevance_score != company.get('ai_robotics_score', 0):
                db.update_company(
                    company['id'],
                    ai_robotics_score=result.relevance_score,
                    matched_keywords=result.matched_keywords,
                    tech_categories=result.tech_categories,
                )
                updated_ai += 1

            progress.advance(task)

    # Calculate startup likelihood scores
    console.print("\n[bold]Step 4: Calculating startup likelihood scores...[/bold]")

    startup_scorer = StartupScorer()
    companies = db.search_companies(source='offeneregister', limit=100000)
    updated_startup = 0

    with Progress(console=console) as progress:
        task = progress.add_task("Scoring startups...", total=len(companies))

        for company in companies:
            ai_score = company.get('ai_robotics_score', 0)
            startup_result = startup_scorer.score_company(
                name=company['name'],
                legal_form=company.get('legal_form'),
                city=company.get('city'),
                ai_relevance_score=ai_score,
            )
            classification = startup_scorer.classify(startup_result, ai_relevance_score=ai_score)

            if (startup_result.total_score != company.get('startup_score', 0) or
                classification != company.get('startup_classification')):
                db.update_company(
                    company['id'],
                    startup_score=startup_result.total_score,
                    startup_classification=classification,
                )
                updated_startup += 1

            progress.advance(task)

    # Summary
    console.print("\n[bold green]Bulk Load Complete![/bold green]")
    console.print("-" * 50)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total records processed", f"{stats.total_records:,}")
    table.add_row("Passed keyword filter", f"{stats.filtered_records:,}")
    table.add_row("Inserted to database", f"{stats.inserted_records:,}")
    table.add_row("Skipped (duplicates)", f"{stats.skipped_duplicates:,}")
    table.add_row("Errors", f"{stats.errors:,}")
    table.add_row("Duration", f"{stats.duration_seconds:.1f} seconds")
    table.add_row("AI scores updated", f"{updated_ai:,}")
    table.add_row("Startup scores updated", f"{updated_startup:,}")

    console.print(table)

    db.close()


@cli.command()
@click.option('--keywords', '-k', multiple=True, help='Keywords to search for')
@click.option('--max-requests', default=50, help='Maximum requests to use (max 60/hr)')
@click.pass_context
def scan(ctx, keywords, max_requests):
    """
    Scan bundesAPI for new companies.

    This queries the official Handelsregister portal with strict
    rate limiting (60 requests/hour legal limit).
    """
    db_path = ctx.obj['db_path']

    if not keywords:
        keywords = [
            "künstliche intelligenz",
            "robotik",
            "machine learning",
            "AI GmbH",
            "KI GmbH",
        ]

    console.print("\n[bold blue]Handelsregister Daily Scan[/bold blue]")
    console.print("=" * 50)
    console.print(f"Keywords: {', '.join(keywords)}")
    console.print(f"Max requests: {max_requests} (legal limit: 60/hr)")
    console.print()

    db = Database(db_path)

    console.print("[yellow]Note:[/yellow] Each search page counts as 1 request.")
    console.print("Scanning may take a while due to rate limiting...\n")

    try:
        stats = create_daily_scan_job(
            db=db,
            keywords=list(keywords),
            max_requests=max_requests,
        )

        console.print("\n[bold green]Scan Complete![/bold green]")
        console.print("-" * 50)

        table = Table(show_header=False)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Companies checked", f"{stats['total_checked']:,}")
        table.add_row("New companies found", f"{stats['new_companies']:,}")
        table.add_row("Requests used", f"{stats['requests_used']:,}")

        console.print(table)

    except Exception as e:
        console.print(f"[red]Error during scan:[/red] {e}")
        raise

    finally:
        db.close()


@cli.command()
@click.pass_context
def report(ctx):
    """Generate summary report."""
    db_path = ctx.obj['db_path']

    db = Database(db_path)
    generator = ReportGenerator(db)

    report_text = generator.generate_summary_report()
    console.print(report_text)

    db.close()


@cli.command()
@click.option('--format', '-f', type=click.Choice(['csv', 'json']), default='csv')
@click.option('--output', '-o', help='Output file path')
@click.option('--min-score', default=1, help='Minimum AI relevance score')
@click.option('--limit', default=10000, help='Maximum companies to export')
@click.pass_context
def export(ctx, format, output, min_score, limit):
    """Export companies to CSV or JSON."""
    db_path = ctx.obj['db_path']

    db = Database(db_path)

    console.print(f"\n[bold]Exporting companies (min score: {min_score})...[/bold]")

    companies = db.search_companies(min_ai_score=min_score, limit=limit)

    if not companies:
        console.print("[yellow]No companies found matching criteria.[/yellow]")
        db.close()
        return

    if format == 'csv':
        exporter = CSVExporter()
        filepath = exporter.export_companies(companies, filename=output)
    else:
        exporter = JSONExporter()
        filepath = exporter.export_companies(companies, filename=output)

    console.print(f"[green]Exported {len(companies):,} companies to:[/green] {filepath}")

    db.close()


@cli.command()
@click.pass_context
def stats(ctx):
    """Show database statistics."""
    db_path = ctx.obj['db_path']

    db = Database(db_path)
    stats = db.get_statistics()

    console.print("\n[bold blue]Database Statistics[/bold blue]")
    console.print("=" * 50)

    # Main stats table
    table = Table(show_header=False)
    table.add_column("Metric", style="cyan", width=30)
    table.add_column("Value", style="green")

    table.add_row("Total companies", f"{stats.get('total_companies', 0):,}")
    table.add_row("Total officers", f"{stats.get('total_officers', 0):,}")
    table.add_row("Total capital events", f"{stats.get('total_capital_events', 0):,}")
    table.add_row("Enrichment queue", f"{stats.get('enrichment_queue_size', 0):,}")

    console.print(table)

    # By source
    console.print("\n[bold]By Source:[/bold]")
    for source, count in stats.get('companies_by_source', {}).items():
        console.print(f"  {source}: {count:,}")

    # By enrichment status
    console.print("\n[bold]By Enrichment Status:[/bold]")
    for status, count in stats.get('companies_by_enrichment', {}).items():
        console.print(f"  {status}: {count:,}")

    # Top cities
    console.print("\n[bold]Top Cities:[/bold]")
    for city, count in stats.get('top_cities', [])[:10]:
        console.print(f"  {city}: {count:,}")

    # AI score distribution
    console.print("\n[bold]AI Score Distribution:[/bold]")
    for score, count in stats.get('ai_score_distribution', []):
        bar = "█" * min(count // 100, 30)
        console.print(f"  Score {score}: {count:>6,} {bar}")

    # Startup classification
    startup_class = stats.get('startup_classification', {})
    if startup_class:
        console.print("\n[bold]Startup Classification:[/bold]")
        for classification, count in startup_class.items():
            emoji = "🚀" if classification == 'startup' else "💼" if classification == 'tech_company' else "🏢"
            console.print(f"  {emoji} {classification}: {count:,}")

    # Startup score distribution
    startup_scores = stats.get('startup_score_distribution', {})
    if startup_scores:
        console.print("\n[bold]Startup Score Distribution:[/bold]")
        for score_range, count in startup_scores.items():
            console.print(f"  {score_range}: {count:,}")

    db.close()


@cli.command()
@click.pass_context
def init(ctx):
    """Initialize database (create tables)."""
    db_path = ctx.obj['db_path']

    console.print(f"Initializing database: {db_path}")
    db = Database(db_path)
    console.print("[green]Database initialized successfully.[/green]")
    db.close()


@cli.command('enrich-officers')
@click.option('--limit', default=None, type=int, help='Limit records to process (for testing)')
@click.pass_context
def enrich_officers(ctx, limit):
    """
    Enrich existing companies with officer data from OffeneRegister bulk file.

    Re-processes the bulk data to add officers for companies that already
    exist in the database. This enables VC partner matching.
    """
    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Officer Data Enrichment[/bold blue]")
    console.print("=" * 50)

    db = Database(db_path)
    source = OffeneRegisterSource()

    # Check current state
    total_companies = db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    current_officers = db.conn.execute("SELECT COUNT(*) FROM officers").fetchone()[0]

    console.print(f"Companies in database: {total_companies:,}")
    console.print(f"Current officers: {current_officers:,}")

    if current_officers > 0:
        if not click.confirm(f"\nDatabase already has {current_officers:,} officers. Continue anyway?"):
            db.close()
            return

    # Check if bulk file exists
    file_info = source.get_file_info()
    if not file_info['exists']:
        console.print("\n[yellow]Bulk data file not found. Downloading...[/yellow]")
        source.download()
    else:
        console.print(f"\nUsing cached file: {file_info['path']}")
        console.print(f"Size: {file_info['size_mb']:.1f} MB")

    console.print("\n[bold]Processing bulk data to extract officers...[/bold]")
    console.print("[dim]This will scan ~5M records to find matches...[/dim]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.fields[matched]:,} matched"),
        console=console,
    ) as progress:
        task = progress.add_task("Processing...", total=None, matched=0)

        def progress_callback(processed, matched):
            progress.update(task, description=f"Processed {processed:,} records", matched=matched)

        stats = source.enrich_officers(
            db=db,
            limit=limit,
            progress_callback=progress_callback,
        )

    # Summary
    console.print("\n[bold green]Officer Enrichment Complete![/bold green]")
    console.print("-" * 50)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total records processed", f"{stats['total_processed']:,}")
    table.add_row("Companies matched", f"{stats['companies_matched']:,}")
    table.add_row("Officers added", f"{stats['officers_added']:,}")
    table.add_row("Already had officers", f"{stats['companies_already_enriched']:,}")
    table.add_row("No officers in source", f"{stats['no_officers_in_source']:,}")

    console.print(table)

    # Show new officer count
    new_officer_count = db.conn.execute("SELECT COUNT(*) FROM officers").fetchone()[0]
    console.print(f"\n[bold]Total officers now: {new_officer_count:,}[/bold]")

    db.close()


@cli.command()
@click.option('--days', default=7, help='Number of days to look back')
@click.pass_context
def new_companies(ctx, days):
    """Show recently discovered companies."""
    db_path = ctx.obj['db_path']

    db = Database(db_path)
    generator = ReportGenerator(db)

    report_text = generator.generate_new_companies_report(days=days)
    console.print(report_text)

    db.close()


@cli.command()
@click.pass_context
def test_connection(ctx):
    """Test OffeneRegister download (first 100 records)."""
    console.print("\n[bold]Testing OffeneRegister connection...[/bold]")

    source = OffeneRegisterSource()
    filter_ = AIRoboticsFilter()

    console.print("Downloading and parsing first 100 records...")

    count = 0
    matches = 0

    for record in source.stream_records(limit=100):
        count += 1
        if filter_.quick_filter(record.name):
            matches += 1
            console.print(f"  [green]Match:[/green] {record.name}")

    console.print(f"\n[bold]Results:[/bold]")
    console.print(f"  Records processed: {count}")
    console.print(f"  AI/Robotics matches: {matches}")
    console.print("\n[green]Connection test successful![/green]")


# ============================================================================
# SCHEDULER COMMANDS
# ============================================================================

@cli.group()
def scheduler():
    """Scheduler commands for continuous monitoring."""
    pass


@scheduler.command('run')
@click.option('--discovery-interval', default=2, help='Hours between discovery runs')
@click.option('--run-now', is_flag=True, help='Run jobs immediately when starting')
@click.pass_context
def scheduler_run(ctx, discovery_interval, run_now):
    """Start the scheduler for continuous monitoring."""
    from scheduler.scheduler import run_scheduler

    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Starting Handelsregister Scheduler[/bold blue]")
    console.print("=" * 50)
    console.print(f"Database: {db_path}")
    console.print(f"Discovery interval: {discovery_interval} hours")
    console.print("Press Ctrl+C to stop\n")

    run_scheduler(
        db_path=db_path,
        discovery_interval=discovery_interval,
        run_discovery_now=run_now,
        run_backfill_now=run_now,
    )


@scheduler.command('status')
@click.pass_context
def scheduler_status(ctx):
    """Show scheduler and rate limiter status."""
    from scheduler.rate_limiter import PersistentRateLimiter

    db_path = ctx.obj['db_path']
    db = Database(db_path)

    console.print("\n[bold blue]Scheduler Status[/bold blue]")
    console.print("=" * 50)

    # Rate limiter status
    rate_limiter = PersistentRateLimiter(db_path)
    rate_state = rate_limiter.get_state()

    console.print("\n[bold]Rate Limiter:[/bold]")
    console.print(f"  Tokens available: {rate_state.tokens_available:.1f} / 60")
    console.print(f"  Requests this hour: {rate_state.requests_this_hour}")
    console.print(f"  Can make request: {'[green]Yes[/green]' if rate_state.can_request else '[red]No[/red]'}")
    if not rate_state.can_request:
        console.print(f"  Wait time: {rate_state.wait_seconds:.0f} seconds")

    # Backfill progress
    try:
        total = db.conn.execute("SELECT COUNT(*) FROM backfill_state").fetchone()[0]
        completed = db.conn.execute(
            "SELECT COUNT(*) FROM backfill_state WHERE status = 'completed'"
        ).fetchone()[0]
        failed = db.conn.execute(
            "SELECT COUNT(*) FROM backfill_state WHERE status = 'failed'"
        ).fetchone()[0]

        console.print("\n[bold]Backfill Progress:[/bold]")
        if total > 0:
            pct = completed / total * 100
            console.print(f"  Completed: {completed}/{total} ({pct:.1f}%)")
            console.print(f"  Failed: {failed}")
        else:
            console.print("  Not initialized")
    except Exception:
        console.print("\n[bold]Backfill Progress:[/bold] Not initialized")

    # Recent jobs
    try:
        recent_jobs = db.conn.execute("""
            SELECT job_type, started_at, status, companies_new, requests_used
            FROM job_runs
            ORDER BY id DESC
            LIMIT 5
        """).fetchall()

        if recent_jobs:
            console.print("\n[bold]Recent Jobs:[/bold]")
            for job in recent_jobs:
                status_color = 'green' if job['status'] == 'completed' else 'yellow'
                console.print(
                    f"  {job['started_at'][:16]} - {job['job_type']}: "
                    f"{job['companies_new']} new, [{status_color}]{job['status']}[/{status_color}]"
                )
    except Exception:
        pass

    db.close()


@scheduler.command('discovery')
@click.option('--max-requests', default=20, help='Maximum requests to use')
@click.option('--dry-run', is_flag=True, help='Run without saving to database')
@click.pass_context
def scheduler_discovery(ctx, max_requests, dry_run):
    """Run a single discovery job."""
    from scheduler.jobs.discovery_job import run_discovery_job

    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Running Discovery Job[/bold blue]")
    console.print("=" * 50)

    stats = run_discovery_job(
        db_path=db_path,
        max_requests=max_requests,
        dry_run=dry_run,
    )

    console.print("\n[bold green]Discovery Complete![/bold green]")

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Companies found", f"{stats['companies_found']:,}")
    table.add_row("New companies", f"{stats['companies_new']:,}")
    table.add_row("Requests used", f"{stats['requests_used']:,}")
    table.add_row("Keywords completed", f"{stats['keywords_completed']}/{stats['keywords_total']}")
    table.add_row("Status", stats['status'])

    console.print(table)


@scheduler.command('backfill')
@click.option('--max-requests', default=30, help='Maximum requests to use')
@click.option('--dry-run', is_flag=True, help='Run without saving to database')
@click.pass_context
def scheduler_backfill(ctx, max_requests, dry_run):
    """Run a single backfill job."""
    from scheduler.jobs.backfill_job import run_backfill_job

    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Running Backfill Job[/bold blue]")
    console.print("=" * 50)

    stats = run_backfill_job(
        db_path=db_path,
        max_requests=max_requests,
        dry_run=dry_run,
    )

    console.print("\n[bold green]Backfill Complete![/bold green]")

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Combinations processed", f"{stats['combinations_processed']:,}")
    table.add_row("Companies found", f"{stats['companies_found']:,}")
    table.add_row("New companies", f"{stats['companies_new']:,}")
    table.add_row("Requests used", f"{stats['requests_used']:,}")
    table.add_row("Overall progress", f"{stats['progress_percent']:.1f}%")

    console.print(table)


@scheduler.command('enrichment')
@click.option('--batch-size', default=50, help='Number of companies to process')
@click.option('--dry-run', is_flag=True, help='Run without saving to database')
@click.pass_context
def scheduler_enrichment(ctx, batch_size, dry_run):
    """Run a single enrichment job (capital detection)."""
    from scheduler.jobs.enrichment_job import run_enrichment_job

    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Running Enrichment Job[/bold blue]")
    console.print("=" * 50)

    stats = run_enrichment_job(
        db_path=db_path,
        batch_size=batch_size,
        dry_run=dry_run,
    )

    console.print("\n[bold green]Enrichment Complete![/bold green]")

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Companies processed", f"{stats['companies_processed']:,}")
    table.add_row("Capital events detected", f"{stats['events_detected']:,}")
    table.add_row("Errors", f"{stats['errors']:,}")
    table.add_row("Queue remaining", f"{stats['queue_remaining']:,}")

    console.print(table)


@scheduler.command('announcements')
@click.option('--lookback-days', default=7, help='Number of days to look back')
@click.option('--max-results', default=500, help='Maximum announcements to fetch')
@click.option('--dry-run', is_flag=True, help='Run without saving to database')
@click.pass_context
def scheduler_announcements(ctx, lookback_days, max_results, dry_run):
    """
    Run announcement monitoring job.

    Fetches recent Registerbekanntmachungen, discovers new AI startups,
    and tracks capital raises for existing companies.
    """
    from scheduler.jobs.announcement_job import run_announcement_job

    db_path = ctx.obj['db_path']

    console.print("\n[bold blue]Running Announcement Monitoring Job[/bold blue]")
    console.print("=" * 50)
    console.print(f"Looking back {lookback_days} days")
    if dry_run:
        console.print("[yellow]DRY RUN - no changes will be saved[/yellow]")

    stats = run_announcement_job(
        db_path=db_path,
        lookback_days=lookback_days,
        max_requests=10,
        dry_run=dry_run,
    )

    console.print("\n[bold green]Announcement Monitoring Complete![/bold green]")

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Announcements fetched", f"{stats['announcements_fetched']:,}")
    table.add_row("Announcements stored", f"{stats['announcements_stored']:,}")
    table.add_row("New AI companies discovered", f"{stats['new_companies']:,}")
    table.add_row("Already tracked", f"{stats['already_tracked']:,}")
    table.add_row("Capital events detected", f"{stats['capital_events']:,}")
    table.add_row("Linked to existing", f"{stats['linked_to_existing']:,}")
    table.add_row("Requests used", f"{stats['requests_used']:,}")
    table.add_row("Errors", f"{stats['errors']:,}")

    console.print(table)

    if stats['new_companies'] > 0:
        console.print(f"\n[bold green]🚀 Discovered {stats['new_companies']} new AI/robotics companies![/bold green]")

    if stats['capital_events'] > 0:
        console.print(f"\n[bold cyan]💰 Detected {stats['capital_events']} capital events![/bold cyan]")


@scheduler.command('investor-detect')
@click.option('--min-confidence', default=0.8, help='Minimum match confidence (0.0-1.0)')
@click.option('--batch-size', default=100, help='Number of records to process per batch')
@click.pass_context
def scheduler_investor_detect(ctx, min_confidence, batch_size):
    """
    Run investor detection job.

    Scans capital events, officers, and announcements for known VCs/investors.
    Creates investment records linking companies to their investors.

    This helps discover relevant companies through their investor connections.
    """
    from scheduler.jobs.investor_detection_job import InvestorDetectionJob

    db_path = ctx.obj['db_path']
    db = Database(db_path)

    console.print("\n[bold blue]Running Investor Detection Job[/bold blue]")
    console.print("=" * 50)
    console.print(f"Min confidence: {min_confidence}")
    console.print(f"Batch size: {batch_size}")

    try:
        job = InvestorDetectionJob(
            db=db,
            batch_size=batch_size,
            min_confidence=min_confidence,
        )
        stats = job.run()

        console.print("\n[bold green]Investor Detection Complete![/bold green]")

        table = Table(show_header=False)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Capital events scanned", f"{stats['capital_events_scanned']:,}")
        table.add_row("Officers scanned", f"{stats['officers_scanned']:,}")
        table.add_row("Announcements scanned", f"{stats['announcements_scanned']:,}")
        table.add_row("Investments found", f"{stats['investments_found']:,}")
        table.add_row("New investments", f"{stats['investments_new']:,}")
        table.add_row("Duration", f"{stats['duration_seconds']:.1f}s")
        table.add_row("Errors", f"{stats['errors']:,}")

        console.print(table)

        if stats['investments_new'] > 0:
            console.print(f"\n[bold green]Found {stats['investments_new']} new investor-company connections![/bold green]")

    finally:
        db.close()


# ============================================================================
# CAPITAL EVENTS COMMANDS
# ============================================================================

@cli.command('capital-events')
@click.option('--days', default=30, help='Show events from last N days')
@click.option('--company-id', type=int, help='Show events for specific company')
@click.pass_context
def capital_events(ctx, days, company_id):
    """Show capital change events (raises, decreases)."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    console.print("\n[bold blue]Capital Events[/bold blue]")
    console.print("=" * 50)

    if company_id:
        events = db.get_capital_events(company_id)
        title = f"Events for company ID {company_id}"
    else:
        events = db.get_recent_capital_events(days=days)
        title = f"Events from last {days} days"

    if not events:
        console.print(f"\n[yellow]No capital events found ({title})[/yellow]")
        db.close()
        return

    console.print(f"\n{title}: {len(events)} events\n")

    table = Table()
    table.add_column("Date", style="cyan")
    table.add_column("Company", style="white")
    table.add_column("Type", style="yellow")
    table.add_column("Change", style="green")
    table.add_column("New Amount", style="blue")
    table.add_column("Confidence", style="dim")

    for event in events[:50]:  # Limit to 50 rows
        event_type = event.get('event_type', 'unknown')
        type_emoji = "📈" if event_type == 'increase' else "📉" if event_type == 'decrease' else "🔹"

        change = event.get('change_amount')
        change_str = f"€{change:,.0f}" if change else "-"

        new_amount = event.get('new_amount')
        new_str = f"€{new_amount:,.0f}" if new_amount else "-"

        confidence = event.get('confidence_score', 0)
        conf_str = f"{confidence:.0%}"

        company_name = event.get('company_name', event.get('name', f"ID:{event.get('company_id')}"))

        table.add_row(
            event.get('event_date', '')[:10] if event.get('event_date') else '-',
            company_name[:40],
            f"{type_emoji} {event_type}",
            change_str,
            new_str,
            conf_str,
        )

    console.print(table)
    db.close()


# ============================================================================
# INVESTOR COMMANDS
# ============================================================================

@cli.group()
def investors():
    """View investor/VC tracking data."""
    pass


@investors.command('list')
@click.option('--limit', default=20, help='Number of investors to show')
@click.pass_context
def investors_list(ctx, limit):
    """List known investors/VCs in the database."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    try:
        rows = db.conn.execute("""
            SELECT i.id, i.canonical_name, i.type, i.headquarters_city,
                   COUNT(inv.id) as investment_count
            FROM investors i
            LEFT JOIN investments inv ON i.id = inv.investor_id
            GROUP BY i.id
            ORDER BY investment_count DESC, i.canonical_name
            LIMIT ?
        """, (limit,)).fetchall()

        if not rows:
            console.print("[yellow]No investors found. Run 'scheduler investor-detect' to seed and scan.[/yellow]")
            return

        table = Table(title=f"Investors ({len(rows)} shown)")
        table.add_column("Name", style="green")
        table.add_column("Type", style="cyan")
        table.add_column("HQ", style="dim")
        table.add_column("Investments", style="yellow")

        for row in rows:
            table.add_row(
                row['canonical_name'][:40],
                row['type'] or '-',
                row['headquarters_city'] or '-',
                str(row['investment_count'])
            )

        console.print(table)

    finally:
        db.close()


@investors.command('portfolio')
@click.argument('investor_name')
@click.pass_context
def investors_portfolio(ctx, investor_name):
    """Show companies in an investor's portfolio."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    try:
        # Find investor by partial name match
        investor = db.conn.execute("""
            SELECT id, canonical_name, type, headquarters_city
            FROM investors
            WHERE canonical_name LIKE ? COLLATE NOCASE
            LIMIT 1
        """, (f"%{investor_name}%",)).fetchone()

        if not investor:
            console.print(f"[red]Investor '{investor_name}' not found[/red]")
            return

        console.print(f"\n[bold blue]{investor['canonical_name']}[/bold blue]")
        console.print(f"Type: {investor['type'] or 'Unknown'} | HQ: {investor['headquarters_city'] or 'Unknown'}")
        console.print("=" * 50)

        # Get portfolio companies
        companies = db.conn.execute("""
            SELECT c.name, c.city, c.ai_robotics_score, c.startup_classification,
                   inv.round_type, inv.amount, inv.confidence, inv.detection_source,
                   inv.investment_date
            FROM investments inv
            JOIN companies c ON inv.company_id = c.id
            WHERE inv.investor_id = ?
            ORDER BY inv.confidence DESC, c.ai_robotics_score DESC
        """, (investor['id'],)).fetchall()

        if not companies:
            console.print("[yellow]No portfolio companies detected yet.[/yellow]")
            return

        table = Table(title=f"Portfolio ({len(companies)} companies)")
        table.add_column("Company", style="green")
        table.add_column("City", style="dim")
        table.add_column("AI Score", style="cyan")
        table.add_column("Round", style="yellow")
        table.add_column("Confidence", style="blue")
        table.add_column("Source", style="dim")

        for company in companies:
            table.add_row(
                company['name'][:35],
                company['city'] or '-',
                str(company['ai_robotics_score'] or 0),
                company['round_type'] or '-',
                f"{company['confidence']:.0%}",
                company['detection_source'] or '-'
            )

        console.print(table)

    finally:
        db.close()


@investors.command('investments')
@click.option('--min-confidence', default=0.8, help='Minimum confidence threshold')
@click.option('--limit', default=50, help='Maximum investments to show')
@click.pass_context
def investors_investments(ctx, min_confidence, limit):
    """Show all detected investments."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    try:
        investments = db.conn.execute("""
            SELECT c.name as company_name, c.city, c.ai_robotics_score,
                   i.canonical_name as investor_name, i.type as investor_type,
                   inv.round_type, inv.amount, inv.confidence,
                   inv.detection_source, inv.investment_date
            FROM investments inv
            JOIN companies c ON inv.company_id = c.id
            JOIN investors i ON inv.investor_id = i.id
            WHERE inv.confidence >= ?
            ORDER BY inv.confidence DESC, inv.detected_at DESC
            LIMIT ?
        """, (min_confidence, limit)).fetchall()

        if not investments:
            console.print("[yellow]No investments found. Run 'scheduler investor-detect' first.[/yellow]")
            return

        table = Table(title=f"Detected Investments ({len(investments)} shown)")
        table.add_column("Company", style="green")
        table.add_column("Investor", style="cyan")
        table.add_column("Type", style="dim")
        table.add_column("Round", style="yellow")
        table.add_column("Confidence", style="blue")

        for inv in investments:
            table.add_row(
                inv['company_name'][:30],
                inv['investor_name'][:25],
                inv['investor_type'] or '-',
                inv['round_type'] or '-',
                f"{inv['confidence']:.0%}"
            )

        console.print(table)

        # Summary
        console.print(f"\n[bold]Summary:[/bold] {len(investments)} investor-company connections detected")

    finally:
        db.close()


@investors.command('stats')
@click.pass_context
def investors_stats(ctx):
    """Show investor detection statistics."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    try:
        # Total investors and investments
        total_investors = db.conn.execute("SELECT COUNT(*) FROM investors").fetchone()[0]
        total_investments = db.conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]

        # Investors with detected investments
        active_investors = db.conn.execute("""
            SELECT COUNT(DISTINCT investor_id) FROM investments
        """).fetchone()[0]

        # Companies with investor connections
        funded_companies = db.conn.execute("""
            SELECT COUNT(DISTINCT company_id) FROM investments
        """).fetchone()[0]

        # By detection source
        by_source = db.conn.execute("""
            SELECT detection_source, COUNT(*) as count
            FROM investments
            GROUP BY detection_source
            ORDER BY count DESC
        """).fetchall()

        # By investor type
        by_type = db.conn.execute("""
            SELECT i.type, COUNT(inv.id) as count
            FROM investments inv
            JOIN investors i ON inv.investor_id = i.id
            GROUP BY i.type
            ORDER BY count DESC
        """).fetchall()

        # Average confidence
        avg_conf = db.conn.execute("""
            SELECT AVG(confidence) FROM investments
        """).fetchone()[0] or 0

        console.print("\n[bold blue]Investor Detection Statistics[/bold blue]")
        console.print("=" * 50)

        table = Table(show_header=False)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Total investors in database", f"{total_investors:,}")
        table.add_row("Investors with detected investments", f"{active_investors:,}")
        table.add_row("Total investments detected", f"{total_investments:,}")
        table.add_row("Companies with investor connections", f"{funded_companies:,}")
        table.add_row("Average detection confidence", f"{avg_conf:.1%}")

        console.print(table)

        if by_source:
            console.print("\n[bold]By Detection Source:[/bold]")
            for row in by_source:
                console.print(f"  {row['detection_source'] or 'unknown'}: {row['count']:,}")

        if by_type:
            console.print("\n[bold]By Investor Type:[/bold]")
            for row in by_type:
                console.print(f"  {row['type'] or 'unknown'}: {row['count']:,}")

    finally:
        db.close()


@investors.command('search')
@click.argument('investor_name')
@click.option('--max-results', default=50, help='Maximum results to fetch')
@click.pass_context
def investors_search(ctx, investor_name, max_results):
    """
    Search Handelsregister for companies where an investor is a shareholder.

    This searches the "Name des Beteiligten" field to find all companies
    where the specified investor appears as a participant/shareholder.

    Example:
        python main.py investors search "Sequoia Capital"
        python main.py investors search "Index Ventures"
    """
    from sources.bundesapi import BundesAPISource

    console.print(f"\n[bold blue]Searching Handelsregister for: {investor_name}[/bold blue]")
    console.print("=" * 50)
    console.print(f"[dim]Searching by shareholder name (Name des Beteiligten)[/dim]\n")

    source = BundesAPISource()
    results = list(source.search(
        keywords=[],  # No company name keywords
        shareholder_name=investor_name,
        max_results=max_results,
    ))

    if not results:
        console.print(f"[yellow]No companies found with '{investor_name}' as shareholder[/yellow]")
        console.print("\n[dim]Note: The investor may use different legal entity names in Germany.[/dim]")
        console.print("[dim]Try searching for specific fund names like 'Sequoia Capital Global Growth Fund'[/dim]")
        return

    console.print(f"[green]Found {len(results)} companies![/green]\n")

    table = Table(title=f"Companies with {investor_name} as Shareholder")
    table.add_column("Company", style="cyan")
    table.add_column("Registry", style="dim")
    table.add_column("Court", style="dim")
    table.add_column("Status", style="green")

    db_path = ctx.obj['db_path']
    db = Database(db_path)

    try:
        for result in results:
            table.add_row(
                result.name,
                f"{result.registry_type} {result.native_company_number}",
                result.registry_court or "",
                result.status or "",
            )

            # Optionally add to database
            existing = db.get_company_by_native_number(result.native_company_number)
            if not existing:
                console.print(f"  [dim]→ New company discovered: {result.name}[/dim]")

        console.print(table)

    finally:
        db.close()


# ============================================================================
# GROUNDTRUTH COMMANDS
# ============================================================================

@cli.group()
def groundtruth():
    """Groundtruth management for filter accuracy tracking."""
    pass


@groundtruth.command('verify')
@click.argument('company_id', type=int)
@click.option('--confirmed', is_flag=True, help='Mark as confirmed AI/robotics company')
@click.option('--false-positive', is_flag=True, help='Mark as false positive')
@click.option('--reason', help='Reason for false positive classification')
@click.pass_context
def groundtruth_verify(ctx, company_id, confirmed, false_positive, reason):
    """Verify a company as AI/robotics or false positive."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    # Ensure groundtruth table exists
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS groundtruth_companies (
            id INTEGER PRIMARY KEY,
            company_id INTEGER UNIQUE NOT NULL,
            is_ai_robotics BOOLEAN NOT NULL,
            confidence TEXT DEFAULT 'verified',
            verification_source TEXT DEFAULT 'manual',
            verified_at TEXT,
            false_positive_reason TEXT,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
    """)

    # Get company info
    company = db.get_company(company_id)
    if not company:
        console.print(f"[red]Company ID {company_id} not found[/red]")
        db.close()
        return

    if not confirmed and not false_positive:
        console.print("[yellow]Please specify --confirmed or --false-positive[/yellow]")
        db.close()
        return

    is_ai = confirmed and not false_positive
    from datetime import datetime
    now = datetime.utcnow().isoformat()

    db.conn.execute("""
        INSERT OR REPLACE INTO groundtruth_companies
        (company_id, is_ai_robotics, confidence, verification_source, verified_at, false_positive_reason)
        VALUES (?, ?, 'verified', 'manual', ?, ?)
    """, (company_id, 1 if is_ai else 0, now, reason))
    db.conn.commit()

    status = "[green]AI/robotics company[/green]" if is_ai else "[red]False positive[/red]"
    console.print(f"\nMarked [bold]{company['name']}[/bold] as {status}")
    if reason:
        console.print(f"Reason: {reason}")

    db.close()


@groundtruth.command('import')
@click.option('--file', 'filepath', required=True, help='CSV file with company names')
@click.option('--column', default='name', help='Column name containing company names')
@click.option('--confirmed', is_flag=True, help='Mark all as confirmed AI/robotics')
@click.pass_context
def groundtruth_import(ctx, filepath, column, confirmed):
    """Import groundtruth from CSV file.

    CSV should have a column with company names. We'll match them
    to companies in our database by fuzzy name matching.

    Example CSV:
    name,notes
    "Aleph Alpha GmbH",Large language models
    "DeepL SE",Neural machine translation
    """
    import csv
    from datetime import datetime

    db_path = ctx.obj['db_path']
    db = Database(db_path)

    # Ensure groundtruth table exists
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS groundtruth_companies (
            id INTEGER PRIMARY KEY,
            company_id INTEGER UNIQUE NOT NULL,
            is_ai_robotics BOOLEAN NOT NULL,
            confidence TEXT DEFAULT 'verified',
            verification_source TEXT DEFAULT 'import',
            verified_at TEXT,
            false_positive_reason TEXT,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        )
    """)

    console.print(f"\n[bold blue]Importing Groundtruth from {filepath}[/bold blue]")
    console.print("=" * 50)

    # Read CSV
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        console.print(f"[red]Error reading file: {e}[/red]")
        db.close()
        return

    if column not in rows[0]:
        console.print(f"[red]Column '{column}' not found. Available: {list(rows[0].keys())}[/red]")
        db.close()
        return

    matched = 0
    not_found = []
    now = datetime.utcnow().isoformat()

    for row in rows:
        name = row[column].strip()
        if not name:
            continue

        # Try exact match first
        company = db.conn.execute(
            "SELECT id, name FROM companies WHERE name = ? COLLATE NOCASE",
            (name,)
        ).fetchone()

        # Try partial match if exact fails
        if not company:
            company = db.conn.execute(
                "SELECT id, name FROM companies WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                (f"%{name}%",)
            ).fetchone()

        if company:
            db.conn.execute("""
                INSERT OR REPLACE INTO groundtruth_companies
                (company_id, is_ai_robotics, confidence, verification_source, verified_at)
                VALUES (?, ?, 'verified', 'import', ?)
            """, (company['id'], 1 if confirmed else 1, now))
            matched += 1
            console.print(f"  [green]✓[/green] {name} → {company['name']}")
        else:
            not_found.append(name)

    db.conn.commit()

    console.print(f"\n[bold]Results:[/bold]")
    console.print(f"  Matched: {matched}")
    console.print(f"  Not found: {len(not_found)}")

    if not_found and len(not_found) <= 20:
        console.print("\n[yellow]Companies not found in database:[/yellow]")
        for name in not_found:
            console.print(f"  • {name}")

    db.close()


@groundtruth.command('report')
@click.pass_context
def groundtruth_report(ctx):
    """Show groundtruth accuracy report."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    console.print("\n[bold blue]Groundtruth Accuracy Report[/bold blue]")
    console.print("=" * 50)

    try:
        # Check if table exists
        db.conn.execute("SELECT 1 FROM groundtruth_companies LIMIT 1")
    except Exception:
        console.print("[yellow]No groundtruth data yet. Use 'groundtruth verify' to add entries.[/yellow]")
        db.close()
        return

    # Get stats
    total = db.conn.execute("SELECT COUNT(*) FROM groundtruth_companies").fetchone()[0]
    confirmed = db.conn.execute(
        "SELECT COUNT(*) FROM groundtruth_companies WHERE is_ai_robotics = 1"
    ).fetchone()[0]
    false_positives = db.conn.execute(
        "SELECT COUNT(*) FROM groundtruth_companies WHERE is_ai_robotics = 0"
    ).fetchone()[0]

    if total == 0:
        console.print("[yellow]No groundtruth entries yet.[/yellow]")
        db.close()
        return

    precision = confirmed / total * 100 if total > 0 else 0

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total verified", f"{total:,}")
    table.add_row("Confirmed AI/robotics", f"{confirmed:,}")
    table.add_row("False positives", f"{false_positives:,}")
    table.add_row("Precision", f"{precision:.1f}%")

    console.print(table)

    # Show recent false positives
    fps = db.conn.execute("""
        SELECT g.*, c.name, c.ai_robotics_score
        FROM groundtruth_companies g
        JOIN companies c ON g.company_id = c.id
        WHERE g.is_ai_robotics = 0
        ORDER BY g.verified_at DESC
        LIMIT 10
    """).fetchall()

    if fps:
        console.print("\n[bold]Recent False Positives:[/bold]")
        for fp in fps:
            reason = fp['false_positive_reason'] or 'No reason given'
            console.print(f"  • {fp['name']} (score: {fp['ai_robotics_score']}) - {reason}")

    db.close()


# =========================================================================
# Announcements Commands
# =========================================================================

@cli.group()
def announcements():
    """Manage Registerbekanntmachungen (register announcements)."""
    pass


@announcements.command('fetch')
@click.option('--date-from', required=True, help='Start date (DD.MM.YYYY)')
@click.option('--date-to', required=True, help='End date (DD.MM.YYYY)')
@click.option('--state', default=None, help='State code (e.g., by, be, nw)')
@click.option('--category', default=None, help='Category: 1=deletion, 2=transformation, 3=new docs, 4=other')
@click.option('--max-results', default=500, help='Maximum results')
@click.pass_context
def announcements_fetch(ctx, date_from, date_to, state, category, max_results):
    """
    Fetch announcements from Registerbekanntmachungen for a date range.

    Example:
        python main.py announcements fetch --date-from 01.01.2026 --date-to 31.01.2026
    """
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    console.print("\n[bold blue]Fetching Registerbekanntmachungen[/bold blue]")
    console.print(f"Date range: {date_from} to {date_to}")
    if state:
        console.print(f"State: {state}")
    if category:
        categories = {'1': 'Deletion', '2': 'Transformation', '3': 'New docs', '4': 'Other'}
        console.print(f"Category: {categories.get(category, category)}")

    source = BundesAPISource()

    # Fetch announcements
    stats = {'total': 0, 'by_type': {}}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching announcements...", total=None)

        for ann in source.search_announcements(
            date_from=date_from,
            date_to=date_to,
            state=state,
            category=category,
            max_results=max_results,
        ):
            stats['total'] += 1
            stats['by_type'][ann.announcement_type] = stats['by_type'].get(ann.announcement_type, 0) + 1

            # Store in database
            db.insert_announcement(
                company_name=ann.company_name,
                native_company_number=ann.native_company_number,
                announcement_type=ann.announcement_type,
                announcement_date=ann.announcement_date,
                text=ann.text,
                capital_old=ann.capital_old,
                capital_new=ann.capital_new,
            )

            progress.update(task, description=f"Fetching... ({stats['total']} fetched)")

    # Show summary
    console.print(f"\n[bold green]Fetched {stats['total']} announcements[/bold green]")

    if stats['by_type']:
        table = Table(title="By Type")
        table.add_column("Type", style="cyan")
        table.add_column("Count", style="green")

        for t, c in sorted(stats['by_type'].items(), key=lambda x: -x[1]):
            table.add_row(t, str(c))

        console.print(table)

    db.close()


@announcements.command('list')
@click.option('--type', 'ann_type', default=None, help='Filter by type (neueintragung, kapitalerhoehung, loeschung, etc.)')
@click.option('--limit', default=20, help='Number of results')
@click.pass_context
def announcements_list(ctx, ann_type, limit):
    """List stored announcements."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    announcements = db.get_announcements(
        announcement_type=ann_type,
        limit=limit,
    )

    if not announcements:
        console.print("[yellow]No announcements found.[/yellow]")
        db.close()
        return

    table = Table(title=f"Announcements ({len(announcements)} shown)")
    table.add_column("Date", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Company", style="green")
    table.add_column("Registry", style="dim")

    for ann in announcements:
        table.add_row(
            ann.get('announcement_date', 'N/A'),
            ann.get('announcement_type', 'N/A'),
            ann.get('company_name', 'N/A')[:40],
            ann.get('native_company_number', 'N/A'),
        )

    console.print(table)
    db.close()


@announcements.command('stats')
@click.pass_context
def announcements_stats(ctx):
    """Show announcement statistics."""
    db_path = ctx.obj['db_path']
    db = Database(db_path)

    total = db.count_announcements()
    by_type = db.get_announcement_stats()

    console.print(f"\n[bold]Total announcements:[/bold] {total:,}")

    if by_type:
        table = Table(title="By Type")
        table.add_column("Type", style="cyan")
        table.add_column("Count", style="green")
        table.add_column("%", style="dim")

        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            pct = (c / total * 100) if total > 0 else 0
            table.add_row(t or 'unknown', f"{c:,}", f"{pct:.1f}%")

        console.print(table)

    db.close()


# ============================================================================
# NEWS MONITORING COMMANDS
# ============================================================================

@cli.group()
def news():
    """News monitoring for startup funding announcements."""
    pass


@news.command('scan')
@click.option('--funding-only', is_flag=True, help='Only show funding-related articles')
@click.option('--ai-only', is_flag=True, help='Only show AI/robotics-related articles')
@click.pass_context
def news_scan(ctx, funding_only, ai_only):
    """Scan RSS feeds for startup news."""
    from sources.news_monitor import NewsMonitor

    console.print("\n[bold blue]Scanning Startup News Feeds[/bold blue]")
    console.print("=" * 50)

    monitor = NewsMonitor()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching feeds...", total=None)
        articles = monitor.fetch_all_articles()
        progress.update(task, description=f"Found {len(articles)} articles")

    if funding_only:
        articles = [a for a in articles if monitor.is_funding_related(a)]
        console.print(f"\n[bold]Funding-related articles: {len(articles)}[/bold]\n")
    elif ai_only:
        articles = [a for a in articles if monitor.is_ai_robotics_related(a)]
        console.print(f"\n[bold]AI/Robotics-related articles: {len(articles)}[/bold]\n")
    else:
        console.print(f"\n[bold]Total articles: {len(articles)}[/bold]\n")

    # Show articles
    table = Table(title="Recent Articles")
    table.add_column("Source", style="cyan", width=15)
    table.add_column("Title", style="white", width=60)
    table.add_column("Funding?", style="green", width=8)
    table.add_column("AI?", style="yellow", width=5)

    for article in articles[:30]:
        is_funding = "Yes" if monitor.is_funding_related(article) else ""
        is_ai = "Yes" if monitor.is_ai_robotics_related(article) else ""

        table.add_row(
            article.source,
            article.title[:58] + "..." if len(article.title) > 60 else article.title,
            is_funding,
            is_ai,
        )

    console.print(table)


@news.command('funding')
@click.pass_context
def news_funding(ctx):
    """Extract funding announcements from news."""
    from sources.news_monitor import NewsMonitor

    console.print("\n[bold blue]Extracting Funding Announcements[/bold blue]")
    console.print("=" * 50)

    monitor = NewsMonitor()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning feeds...", total=None)
        funding_mentions = monitor.scan_for_funding()

    if not funding_mentions:
        console.print("[yellow]No funding announcements found in recent news.[/yellow]")
        return

    console.print(f"\n[bold green]Found {len(funding_mentions)} funding mentions![/bold green]\n")

    table = Table(title="Funding Announcements")
    table.add_column("Company", style="green", width=22)
    table.add_column("Amount", style="cyan", width=12)
    table.add_column("Round", style="yellow", width=10)
    table.add_column("Investors", style="white", width=25)
    table.add_column("Conf", style="dim", width=5)
    table.add_column("Source", style="dim", width=12)

    for mention in funding_mentions:
        amount_str = ""
        if mention.amount:
            if mention.amount >= 1_000_000_000:
                amount_str = f"{mention.amount / 1_000_000_000:.1f}B {mention.currency or ''}"
            elif mention.amount >= 1_000_000:
                amount_str = f"{mention.amount / 1_000_000:.1f}M {mention.currency or ''}"
            else:
                amount_str = f"{mention.amount:,.0f} {mention.currency or ''}"

        investors_str = ", ".join(mention.investors[:3])
        if len(mention.investors) > 3:
            investors_str += f" +{len(mention.investors) - 3}"

        conf_str = f"{mention.confidence:.0%}" if hasattr(mention, 'confidence') else "-"

        table.add_row(
            mention.company_name[:20] if mention.company_name else "-",
            amount_str or "-",
            mention.round_type or "-",
            investors_str or "-",
            conf_str,
            mention.source,
        )

    console.print(table)

    # Show article titles
    console.print("\n[bold]Article Sources:[/bold]")
    for mention in funding_mentions[:10]:
        console.print(f"  [dim]{mention.article_title[:70]}...[/dim]")
        console.print(f"    {mention.article_url}")


@news.command('early-stage')
@click.pass_context
def news_early_stage(ctx):
    """Scan for early-stage signals: grants, stipends, spinoffs, accelerators."""
    from sources.news_monitor import NewsMonitor

    console.print("\n[bold blue]Scanning for Early-Stage Signals[/bold blue]")
    console.print("=" * 50)

    monitor = NewsMonitor()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning feeds...", total=None)
        articles = monitor.scan_for_early_stage()

    if not articles:
        console.print("[yellow]No early-stage signals found in recent news.[/yellow]")
        return

    console.print(f"\n[bold green]Found {len(articles)} articles with early-stage signals![/bold green]\n")

    table = Table(title="Early-Stage / Grant / Spinoff Signals")
    table.add_column("Source", style="cyan", width=18)
    table.add_column("Title", style="white", width=60)
    table.add_column("Funding?", style="green", width=8)

    for article in articles[:30]:
        is_funding = "Yes" if monitor.is_funding_related(article) else ""
        table.add_row(
            article.source,
            article.title[:58] + "..." if len(article.title) > 60 else article.title,
            is_funding,
        )

    console.print(table)


if __name__ == '__main__':
    cli(obj={})
