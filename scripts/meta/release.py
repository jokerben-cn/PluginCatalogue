from typing import List, Optional, Dict, TYPE_CHECKING

from mcdreforged.plugin.meta.version import Version

from common import constants, log
from common.report import reporter
from meta.cache import ReleasePageResponse
from meta.plugin import MetaInfo
from utils import value_utils
from utils.serializer import Serializable

if TYPE_CHECKING:
	from plugin.plugin import Plugin
	from plugin.cache import RequestCacheManager


class AssetInfo(Serializable):
	id: int  # GitHub asset ID
	name: str
	size: int
	download_count: int
	created_at: str
	browser_download_url: str


class _GitHubReleaseJson(Serializable):
	html_url: str
	name: str
	tag_name: str
	created_at: str
	body: Optional[str]
	prerelease: bool
	assets: List[AssetInfo]


class _InvalidReleaseError(Exception):
	pass


class ReleaseInfo(Serializable):
	url: str
	name: str
	tag_name: str
	created_at: str
	description: str
	prerelease: bool

	asset: AssetInfo
	meta: MetaInfo

	@classmethod
	def create_from(cls, plugin: 'Plugin', cache_manager: 'RequestCacheManager', js: _GitHubReleaseJson) -> 'ReleaseInfo':
		if js.prerelease:
			raise _InvalidReleaseError('pre-release')

		info = cls()
		info.url = js.html_url
		info.name = js.name
		info.tag_name = js.tag_name
		info.created_at = js.created_at
		info.body = js.body
		info.prerelease = js.prerelease
		info.description = js.body or 'N/A'

		for asset in js.assets:
			if asset.name.endswith('.mcdr') or asset.name.endswith('.pyz'):
				info.asset = asset
				info.meta = cache_manager.fetch_asset_meta(asset.id, asset.browser_download_url)
				break
		else:
			raise _InvalidReleaseError('no valid asset')

		tag_version = cls.__parse_version(js.tag_name, plugin.id)
		meta_version = info.meta.version

		t_ver = Version(tag_version, allow_wildcard=False)
		try:
			m_ver = Version(meta_version, allow_wildcard=False)
		except ValueError as e:
			log.warning('({}) Bad meta version {!r} for tag {!r}: {}'.format(plugin.id, meta_version, info.tag_name, e))
			reporter.record_warning(plugin.id, 'Bad meta version {!r} for tag {!r}'.format(meta_version, info.tag_name), e)
			raise _InvalidReleaseError('bad meta version')

		t_ver_seq = '.'.join(map(str, t_ver.component))
		m_ver_seq = '.'.join(map(str, m_ver.component))
		if not m_ver_seq.startswith(t_ver_seq):
			log.warning('({}) Tag {!r} version {!r} does not match meta version {!r}'.format(plugin.id, js.tag_name, tag_version, meta_version))
			reporter.record_warning(plugin.id, 'Tag {!r} version {!r} does not match meta version {!r}'.format(js.tag_name, tag_version, meta_version))
			raise _InvalidReleaseError('version mismatched')
		elif m_ver != t_ver:
			# TODO: further check for this case
			log.warning('({}) Mismatched but ok version: tag version {!r}, meta version {!r}'.format(plugin.id, tag_version, meta_version))

		return info

	@classmethod
	def __parse_version(cls, tag_name: str, plugin_id: str) -> Optional[str]:
		# Possible tag names
		#   plugin_id-v1.2.3
		#   plugin_id-1.2.3
		#   v1.2.3
		#   1.2.3

		def test_and_return(version_str: str) -> Optional[str]:
			try:
				Version(version_str, allow_wildcard=False)
			except:
				return None
			else:
				return version_str

		version = tag_name
		if version.startswith(plugin_id + '-'):
			version = value_utils.remove_prefix(version, plugin_id + '-')
		if len(version) == 0:
			return version
		if version[0].isdigit():
			return test_and_return(version)
		elif version[0].lower() == 'v':
			return test_and_return(version[1:])
		else:
			return None


class ReleaseSummary(Serializable):
	"""
	/<plugin_id>/release.json
	"""
	schema_version: int
	id: str
	latest_version: Optional[str]
	releases: List[ReleaseInfo]

	@classmethod
	def create_for(cls, plugin: 'Plugin', cache_manager: 'RequestCacheManager') -> 'ReleaseSummary':
		rs = cls()
		rs.schema_version = constants.RELEASE_INFO_SCHEMA_VERSION
		rs.id = plugin.id

		page_map: Dict[int, ReleasePageResponse] = {}  # page index -> page

		# GitHub: Only the first 10000 results are available.
		# 10000 results == 100 pages
		for i in range(100):
			i += 1  # page index starts at 1
			log.info('({}) Fetching release page {}'.format(plugin.id, i))
			page = cache_manager.fetch_release_page(page=i, per_page=constants.MAX_RELEASE_PER_PAGE)
			page_map[i] = page
			if page.empty:
				break

		versions: List[Version] = []
		releases: Dict[str, ReleaseInfo] = {}
		for i, page in value_utils.sort_dict(page_map).items():
			log.info('({}) Checking release page {} with {} release'.format(plugin.id, i, len(page.get_release_data_list())))
			for item in page.get_release_data_list():
				try:
					data = _GitHubReleaseJson.deserialize(item)
					r_info = ReleaseInfo.create_from(plugin, cache_manager, data)
				except _InvalidReleaseError:
					pass
				except Exception as e:
					log.error('Failed to deserialize fetched ReleaseInfo from {}: {}'.format(item, e))
					continue
				else:
					releases[r_info.tag_name] = r_info
					versions.append(Version(r_info.meta.version))

		rs.releases = list(releases.values())
		versions.sort(reverse=True)
		if len(versions) > 0:
			rs.latest_version = str(versions[0])
		else:
			rs.latest_version = None
		return rs

	def get_total_downloads(self) -> int:
		total = 0
		for release in self.releases:
			total += release.asset.download_count
		return total
