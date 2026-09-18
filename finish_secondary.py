"""Finish optional notebook copies on the host, where Osiris is mounted."""
import json
import os
from pathlib import Path
from legacy_archive import handle_request
from secondary_archive import destination_path, requests_from_config


def main():
    result_path = Path('secondary-archive-result.json')
    try:
        results = json.loads(result_path.read_text())
        requests = requests_from_config(json.loads(Path('config.json').read_text()))
        if len(results['requests']) != len(requests):
            raise ValueError('Secondary archive result does not match requests')
        for request, result in zip(requests, results['requests']):
            if request.get('datatype') and not request.get('validator') and os.environ.get('SECONDARY_ARCHIVE_KEEP_GROUPANALYSIS', '1') == '1':
                if not os.environ.get('SECONDARY_ARCHIVE'):
                    raise ValueError('Group-analysis compatibility requires SECONDARY_ARCHIVE')
                print('Updating Osiris compatibility copy for group-analysis output', flush=True)
                handle_request(request['src'], destination_path(request))
                result['groupanalysis_compatibility_copy'] = True
        results['complete'] = True
        result_path.write_text(json.dumps(results, indent=2) + '\n')
        if results['requests'] and all(item.get('status') == 'empty' for item in results['requests']):
            print('No secondary files were generated', flush=True)
        else:
            print('Secondary archive completed successfully', flush=True)
    except Exception:
        if result_path.exists():
            result_path.unlink()
        raise


if __name__ == '__main__':
    main()
