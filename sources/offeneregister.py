"""
OffeneRegister.de bulk data loader.

Downloads and parses the complete German company register data
from the OffeneRegister.de open data project.

Data source: https://daten.offeneregister.de/
License: CC-BY-4.0 (requires attribution to OpenCorporates)
"""

import bz2
import json
import urllib.request
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, List, Dict, Callable, Any
from dataclasses import dataclass


@dataclass
class CompanyRecord:
    """Standardized company record from OffeneRegister."""
    company_number: str
    native_company_number: Optional[str]
    name: str
    legal_form: Optional[str]
    current_status: Optional[str]
    registry_court: Optional[str]
    registry_type: Optional[str]
    registration_date: Optional[str]
    street: Optional[str]
    postal_code: Optional[str]
    city: Optional[str]
    state: Optional[str]
    officers: List[Dict]
    retrieved_at: str


@dataclass
class LoadStats:
    """Statistics from a bulk load operation."""
    total_records: int
    filtered_records: int
    inserted_records: int
    skipped_duplicates: int
    errors: int
    duration_seconds: float


class OffeneRegisterSource:
    """
    Bulk data source from OffeneRegister.de.

    Downloads and parses the ~260MB bz2 compressed JSONL file
    containing ~5M German company records.
    """

    DOWNLOAD_URL = "https://daten.offeneregister.de/de_companies_ocdata.jsonl.bz2"

    def __init__(self, cache_dir: Path = None):
        if cache_dir is None:
            cache_dir = Path("./data")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._local_file = self.cache_dir / "de_companies_ocdata.jsonl.bz2"

    def download(self, force: bool = False, progress_callback: Callable[[int, int], None] = None) -> Path:
        """
        Download bulk data file if not cached or forced.

        Args:
            force: Force re-download even if cached
            progress_callback: Callback(bytes_downloaded, total_bytes) for progress

        Returns:
            Path to the downloaded file
        """
        if not force and self._local_file.exists():
            print(f"Using cached file: {self._local_file}")
            return self._local_file

        print(f"Downloading from {self.DOWNLOAD_URL}...")
        print("This is approximately 260MB and may take several minutes.")

        # Get file size
        with urllib.request.urlopen(self.DOWNLOAD_URL) as response:
            total_size = int(response.headers.get('Content-Length', 0))

        # Download with progress
        def reporthook(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if progress_callback:
                progress_callback(downloaded, total_size)
            elif total_size > 0:
                percent = min(100, (downloaded / total_size) * 100)
                print(f"\rDownloading: {percent:.1f}% ({downloaded / 1024 / 1024:.1f} MB)", end='', flush=True)

        urllib.request.urlretrieve(self.DOWNLOAD_URL, self._local_file, reporthook)
        print("\nDownload complete!")

        return self._local_file

    def stream_records(self, limit: Optional[int] = None) -> Iterator[CompanyRecord]:
        """
        Stream-parse the JSONL file.

        Memory efficient: yields one record at a time.

        Args:
            limit: Maximum number of records to yield (for testing)

        Yields:
            CompanyRecord objects
        """
        filepath = self.download()

        count = 0
        with bz2.open(filepath, 'rt', encoding='utf-8') as f:
            for line in f:
                if limit and count >= limit:
                    break

                if not line.strip():
                    continue

                try:
                    data = json.loads(line)
                    record = self._parse_record(data)
                    if record:
                        yield record
                        count += 1
                except json.JSONDecodeError as e:
                    print(f"JSON parse error: {e}")
                    continue
                except Exception as e:
                    print(f"Error parsing record: {e}")
                    continue

    def _parse_record(self, data: Dict) -> Optional[CompanyRecord]:
        """Parse raw JSON record into CompanyRecord."""
        # Parse officers
        officers = []
        for officer_wrapper in data.get('officers', []):
            officer = officer_wrapper.get('officer', {})
            officers.append({
                'name': officer.get('name', ''),
                'role': officer.get('position'),
                'start_date': officer.get('start_date'),
                'end_date': officer.get('end_date'),
                'is_current': officer.get('end_date') is None,
                'city': officer.get('other_attributes', {}).get('city'),
            })

        # Parse address
        addr_str = data.get('registered_address', '')
        addr_parts = self._parse_address(addr_str)

        # Infer registration date from earliest officer
        registration_date = self._infer_registration_date(officers)

        # Parse native company number for court and type
        native_number = data.get('native_company_number', '')
        registry_court, registry_type = self._parse_native_number(native_number)

        # Extract legal form from name
        legal_form = self._extract_legal_form(data.get('name', ''))

        return CompanyRecord(
            company_number=data.get('company_number', ''),
            native_company_number=native_number,
            name=data.get('name', ''),
            legal_form=legal_form,
            current_status=data.get('current_status'),
            registry_court=registry_court,
            registry_type=registry_type,
            registration_date=registration_date,
            street=addr_parts.get('street'),
            postal_code=addr_parts.get('postal_code'),
            city=addr_parts.get('city'),
            state=data.get('all_attributes', {}).get('federal_state'),
            officers=officers,
            retrieved_at=data.get('retrieved_at', datetime.now().isoformat()),
        )

    def _parse_address(self, addr_str: str) -> Dict[str, Optional[str]]:
        """
        Parse address string into components.

        OffeneRegister addresses are typically formatted as:
        "Street Number, Postal City" or similar
        """
        if not addr_str:
            return {'street': None, 'postal_code': None, 'city': None}

        parts = addr_str.split(',')

        result = {
            'street': parts[0].strip() if len(parts) > 0 else None,
            'postal_code': None,
            'city': None,
        }

        if len(parts) > 1:
            # Try to extract postal code and city from last part
            location = parts[-1].strip()
            # German postal codes are 5 digits
            import re
            match = re.match(r'^(\d{5})\s+(.+)$', location)
            if match:
                result['postal_code'] = match.group(1)
                result['city'] = match.group(2)
            else:
                result['city'] = location

        return result

    def _infer_registration_date(self, officers: List[Dict]) -> Optional[str]:
        """Infer registration date from earliest officer start date."""
        dates = []
        for officer in officers:
            start_date = officer.get('start_date')
            if start_date:
                try:
                    dates.append(datetime.fromisoformat(start_date.replace('Z', '+00:00')))
                except (ValueError, TypeError):
                    pass

        if dates:
            return min(dates).isoformat()
        return None

    def _parse_native_number(self, native_number: str) -> tuple:
        """
        Extract registry court and type from native company number.

        Format examples:
        - "Amtsgericht München HRB 123456"
        - "München HRB 123456"
        """
        if not native_number:
            return None, None

        registry_court = None
        registry_type = None

        # Extract registry type
        for reg_type in ['HRB', 'HRA', 'GnR', 'PR', 'VR']:
            if reg_type in native_number.upper():
                registry_type = reg_type
                break

        # Extract court name
        parts = native_number.split()
        if len(parts) >= 2:
            if parts[0].lower() == 'amtsgericht':
                registry_court = f"Amtsgericht {parts[1]}"
            else:
                # First part might be the city
                registry_court = parts[0]

        return registry_court, registry_type

    def _extract_legal_form(self, name: str) -> Optional[str]:
        """Extract legal form from company name."""
        if not name:
            return None

        # Common German legal forms (check longer forms first)
        legal_forms = [
            'GmbH & Co. KG',
            'GmbH & Co. KGaA',
            'UG (haftungsbeschränkt) & Co. KG',
            'UG (haftungsbeschränkt)',
            'GmbH',
            'AG',
            'KGaA',
            'KG',
            'OHG',
            'UG',
            'e.V.',
            'eV',
            'GbR',
            'SE',
        ]

        name_upper = name.upper()
        for form in legal_forms:
            if form.upper() in name_upper:
                return form

        return None

    def load_to_database(
        self,
        db: 'Database',
        filter_func: Optional[Callable[[CompanyRecord], bool]] = None,
        batch_size: int = 1000,
        limit: Optional[int] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> LoadStats:
        """
        Load records into database with optional filtering.

        Args:
            db: Database instance
            filter_func: Optional filter function (return True to include)
            batch_size: Number of records to insert per batch
            limit: Maximum records to process (for testing)
            progress_callback: Callback(records_processed) for progress

        Returns:
            LoadStats with operation statistics
        """
        from datetime import datetime
        start_time = datetime.now()

        stats = {
            'total_records': 0,
            'filtered_records': 0,
            'inserted_records': 0,
            'skipped_duplicates': 0,
            'errors': 0,
        }

        batch = []

        for record in self.stream_records(limit=limit):
            stats['total_records'] += 1

            # Apply filter
            if filter_func and not filter_func(record):
                continue

            stats['filtered_records'] += 1
            batch.append(record)

            # Insert batch
            if len(batch) >= batch_size:
                inserted, skipped, errors = self._insert_batch(db, batch)
                stats['inserted_records'] += inserted
                stats['skipped_duplicates'] += skipped
                stats['errors'] += errors
                batch = []

                if progress_callback:
                    progress_callback(stats['total_records'])

        # Insert remaining records
        if batch:
            inserted, skipped, errors = self._insert_batch(db, batch)
            stats['inserted_records'] += inserted
            stats['skipped_duplicates'] += skipped
            stats['errors'] += errors

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        return LoadStats(
            total_records=stats['total_records'],
            filtered_records=stats['filtered_records'],
            inserted_records=stats['inserted_records'],
            skipped_duplicates=stats['skipped_duplicates'],
            errors=stats['errors'],
            duration_seconds=duration,
        )

    def _insert_batch(self, db: 'Database', batch: List[CompanyRecord]) -> tuple:
        """Insert a batch of records into database."""
        inserted = 0
        skipped = 0
        errors = 0

        for record in batch:
            try:
                # Check if already exists
                existing = db.get_company_by_number(record.company_number)
                if existing:
                    skipped += 1
                    continue

                # Insert company
                company_id = db.insert_company(
                    company_number=record.company_number,
                    name=record.name,
                    source='offeneregister',
                    native_company_number=record.native_company_number,
                    legal_form=record.legal_form,
                    current_status=record.current_status,
                    registry_court=record.registry_court,
                    registry_type=record.registry_type,
                    registration_date=record.registration_date,
                    street=record.street,
                    postal_code=record.postal_code,
                    city=record.city,
                    state=record.state,
                )

                # Insert officers
                for officer in record.officers:
                    if officer.get('name'):
                        db.insert_officer(
                            company_id=company_id,
                            name=officer['name'],
                            role=officer.get('role'),
                            start_date=officer.get('start_date'),
                            end_date=officer.get('end_date'),
                            is_current=officer.get('is_current', True),
                        )

                inserted += 1

            except Exception as e:
                errors += 1
                print(f"Error inserting {record.name}: {e}")

        return inserted, skipped, errors

    def get_file_info(self) -> Dict[str, Any]:
        """Get information about the cached file."""
        if not self._local_file.exists():
            return {'exists': False}

        stat = self._local_file.stat()
        return {
            'exists': True,
            'path': str(self._local_file),
            'size_bytes': stat.st_size,
            'size_mb': stat.st_size / 1024 / 1024,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
