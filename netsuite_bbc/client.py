import logging
import re
import warnings
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, List, Sequence, Union

import requests
import zeep
from zeep.cache import SqliteCache
from zeep.xsd.valueobjects import CompoundValue
from zeep.plugins import HistoryPlugin

from . import constants, helpers, passport
from .config import Config
from .util import cached_property

logger = logging.getLogger(__name__)


def WebServiceCall(
    path: str = None,
    extract: Callable = None,
    *,
    default: Any = constants.NOT_SET,
) -> Callable:
    """
    Decorator for NetSuite methods returning SOAP responses

    Args:
        path:
            A dot-separated path for specifying where relevant data resides.
        extract:
            A function to extract data from response before returning it.
        default:
            If the existing path does not exist in response, return this
            instead.

    Returns:
        Decorator to use on `NetSuite` web service methods
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(self, *args, **kw):
            response = fn(self, *args, **kw)

            if path is not None:
                for part in path.split('.'):
                    try:
                        response = getattr(response, part)
                    except AttributeError:
                        if default is constants.NOT_SET:
                            raise
                        else:
                            return default

            if extract is not None:
                response = extract(response)

            return response
        return wrapper
    return decorator


class NetSuite:
    version = '2018.2.0'
    company_url_tmpl = 'https://{account_id}.suitetalk.api.netsuite.com'
    wsdl_url_tmpl = 'https://{account_id}.suitetalk.api.netsuite.com/wsdl/v{underscored_version}/netsuite.wsdl'

    def __repr__(self) -> str:
        return f'<NetSuite {self.hostname}({self.version})>'

    def __init__(
        self,
        config: Union[Config, Dict],
        *,
        version: str = None,
        wsdl_url: str = None,
        use_account_specific_endpoint: bool = False,
        cache: zeep.cache.Base = None,
        session: requests.Session = None,
        sandbox: bool = None,
        max_history_length: int = 10,
    ) -> None:

        if sandbox is not None:
            warnings.warn(
                'The `sandbox` flag has been deprecated and no longer has '
                'any effect. Please locate the correct account ID for your '
                'sandbox instead (usually `_SB1`)',
                DeprecationWarning,
            )

        if version is not None:
            assert re.match(r'\d+\.\d+\.\d+', version)
            self.version = version

        self.__config = self._make_config(config)
        self.__wsdl_url = wsdl_url
        self.__use_account_specific_endpoint = use_account_specific_endpoint
        self.__cache = cache
        self.__session = session

        self.history = HistoryPlugin(maxlen=max_history_length)

    @cached_property
    def wsdl_url(self) -> str:
        return self.__wsdl_url or self._generate_wsdl_url()

    @cached_property
    def company_url(self) -> str:
        return self._generate_company_url()

    @cached_property
    def cache(self) -> zeep.cache.Base:
        return self.__cache or self._generate_cache()

    @cached_property
    def session(self) -> requests.Session:
        return self.__session or self._generate_session()

    @cached_property
    def client(self) -> zeep.Client:
        return self._generate_client()

    @cached_property
    def transport(self):
        return self._generate_transport()

    @property
    def config(self) -> Config:
        return self.__config

    @cached_property
    def hostname(self) -> str:
        return self.wsdl_url.replace('https://', '').partition('/')[0]

    @property
    def service(self) -> zeep.client.ServiceProxy:
        if self.__use_account_specific_endpoint:
            return self.__service_override

        return self.client.service

    @cached_property
    def __service_override(self) -> zeep.client.ServiceProxy:
        default_service = self.client.service
        default_path = default_service._binding_options.get('address').split('.com/')[1]

        account_specific_endpoint = f'{self.company_url}/{default_path}'

        return self.client.create_service(default_service._binding.name, account_specific_endpoint)

    def _make_config(
        self,
        values_obj: Dict
    ) -> Config:
        if isinstance(values_obj, Config):
            return values_obj
        return Config(**values_obj)

    @property
    def underscored_version(self) -> str:
        return self.version.replace('.', '_')

    @property
    def underscored_version_no_micro(self) -> str:
        return self.underscored_version.rpartition('_')[0]

    def _generate_wsdl_url(self) -> str:
        return self.wsdl_url_tmpl.format(
            underscored_version=self.underscored_version,
            # https://followingnetsuite.wordpress.com/2018/10/18/suitetalk-sandbox-urls-addendum/
            account_id=self.config.account.lower().replace('_', '-'),
        )

    def _generate_company_url(self) -> str:
        return self.company_url_tmpl.format(
            # https://followingnetsuite.wordpress.com/2018/10/18/suitetalk-sandbox-urls-addendum/
            account_id=self.config.account.lower().replace('_', '-'),
        )

    def _generate_cache(self) -> zeep.cache.Base:
        return SqliteCache(timeout=60 * 60 * 24 * 365)

    def _generate_session(self) -> requests.Session:
        return requests.Session()

    def _generate_transport(self) -> zeep.transports.Transport:
        return zeep.transports.Transport(
            session=self.session,
            cache=self.cache,
        )

    def generate_passport(self) -> Dict:
        return passport.make(self, self.config)

    def to_builtin(self, obj, *args, **kw):
        """Turn zeep XML object into python built-in data structures"""
        return helpers.to_builtin(obj, *args, **kw)

    @staticmethod
    def _set_default_soapheaders(
        client: zeep.Client,
        preferences: dict = None
    ) -> None:
        client.set_default_soapheaders({
            # https://netsuite.custhelp.com/app/answers/detail/a_id/40934
            # (you need to be logged in to SuiteAnswers for this link to work)
            # 'preferences': {
            #     'warningAsError': True/False,
            #     'disableMandatoryCustomFieldValidation': True/False,
            #     'disableSystemNotesForCustomFields': True/False,
            #     'ignoreReadOnlyFields': True/False,
            #     'runServerSuiteScriptAndTriggerWorkflows': True/False,
            # },
        })

    def _generate_client(self) -> zeep.Client:
        client = zeep.Client(
            self.wsdl_url,
            transport=self.transport,
            plugins=[self.history],
        )
        self._set_default_soapheaders(
            client,
            preferences=self.config.preferences,
        )
        return client

    def _get_namespace(self, name: str, sub_namespace: str) -> str:
        return (
            'urn:{name}_{version}.{sub_namespace}.webservices.netsuite.com'
            .format(
                name=name,
                version=self.underscored_version_no_micro,
                sub_namespace=sub_namespace,
            )
        )

    def _type_factory(
        self,
        name: str,
        sub_namespace: str
    ) -> zeep.client.Factory:
        return self.client.type_factory(
            self._get_namespace(name, sub_namespace)
        )

    @cached_property
    def Core(self) -> zeep.client.Factory:
        return self._type_factory('core', 'platform')

    @cached_property
    def CoreTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.core', 'platform')

    @cached_property
    def FaultsTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.faults', 'platform')

    @cached_property
    def Faults(self) -> zeep.client.Factory:
        return self._type_factory('faults', 'platform')

    @cached_property
    def Messages(self) -> zeep.client.Factory:
        return self._type_factory('messages', 'platform')

    @cached_property
    def Common(self) -> zeep.client.Factory:
        return self._type_factory('common', 'platform')

    @cached_property
    def CommonTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.common', 'platform')

    @cached_property
    def Scheduling(self) -> zeep.client.Factory:
        return self._type_factory('scheduling', 'activities')

    @cached_property
    def SchedulingTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.scheduling', 'activities')

    @cached_property
    def Communication(self) -> zeep.client.Factory:
        return self._type_factory('communication', 'general')

    @cached_property
    def CommunicationTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.communication', 'general')

    @cached_property
    def Filecabinet(self) -> zeep.client.Factory:
        return self._type_factory('filecabinet', 'documents')

    @cached_property
    def FilecabinetTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.filecabinet', 'documents')

    @cached_property
    def Relationships(self) -> zeep.client.Factory:
        return self._type_factory('relationships', 'lists')

    @cached_property
    def RelationshipsTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.relationships', 'lists')

    @cached_property
    def Support(self) -> zeep.client.Factory:
        return self._type_factory('support', 'lists')

    @cached_property
    def SupportTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.support', 'lists')

    @cached_property
    def Accounting(self) -> zeep.client.Factory:
        return self._type_factory('accounting', 'lists')

    @cached_property
    def AccountingTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.accounting', 'lists')

    @cached_property
    def Sales(self) -> zeep.client.Factory:
        return self._type_factory('sales', 'transactions')

    @cached_property
    def SalesTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.sales', 'transactions')

    @cached_property
    def Purchases(self) -> zeep.client.Factory:
        return self._type_factory('purchases', 'transactions')

    @cached_property
    def PurchasesTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.purchases', 'transactions')

    @cached_property
    def Customers(self) -> zeep.client.Factory:
        return self._type_factory('customers', 'transactions')

    @cached_property
    def CustomersTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.customers', 'transactions')

    @cached_property
    def Financial(self) -> zeep.client.Factory:
        return self._type_factory('financial', 'transactions')

    @cached_property
    def FinancialTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.financial', 'transactions')

    @cached_property
    def Bank(self) -> zeep.client.Factory:
        return self._type_factory('bank', 'transactions')

    @cached_property
    def BankTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.bank', 'transactions')

    @cached_property
    def Inventory(self) -> zeep.client.Factory:
        return self._type_factory('inventory', 'transactions')

    @cached_property
    def InventoryTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.inventory', 'transactions')

    @cached_property
    def General(self) -> zeep.client.Factory:
        return self._type_factory('general', 'transactions')

    @cached_property
    def Customization(self) -> zeep.client.Factory:
        return self._type_factory('customization', 'setup')

    @cached_property
    def CustomizationTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.customization', 'setup')

    @cached_property
    def Employees(self) -> zeep.client.Factory:
        return self._type_factory('employees', 'lists')

    @cached_property
    def EmployeesTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.employees', 'lists')

    @cached_property
    def Website(self) -> zeep.client.Factory:
        return self._type_factory('website', 'lists')

    @cached_property
    def WebsiteTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.website', 'lists')

    @cached_property
    def EmployeesTransactions(self) -> zeep.client.Factory:
        return self._type_factory('employees', 'transactions')

    @cached_property
    def EmployeesTransactionsTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.employees', 'transactions')

    @cached_property
    def Marketing(self) -> zeep.client.Factory:
        return self._type_factory('marketing', 'lists')

    @cached_property
    def MarketingTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.marketing', 'lists')

    @cached_property
    def DemandPlanning(self) -> zeep.client.Factory:
        return self._type_factory('demandplanning', 'transactions')

    @cached_property
    def DemandPlanningTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.demandplanning', 'transactions')

    @cached_property
    def SupplyChain(self) -> zeep.client.Factory:
        return self._type_factory('supplychain', 'lists')

    @cached_property
    def SupplyChainTypes(self) -> zeep.client.Factory:
        return self._type_factory('types.supplychain', 'lists')

    def request(
        self,
        service_name: str,
        *args,
        **kw
    ) -> zeep.xsd.ComplexType:
        """
        Make a web service request to NetSuite

        Args:
            service_name:
                The NetSuite service to call
        Returns:
            The response from NetSuite
        """
        svc = getattr(self.service, service_name)
        return svc(*args, _soapheaders=self.generate_passport(), **kw)

    def search(
        self,
        searchRecord: dict,
        returnAll: bool = False,
    ) -> List:
        """
        Search for NetSuite records using `searchRecord` dictionary.

        Args:
            searchRecord: Dictionary containing search parameters
                defined and formatted by the NetSuite WSDL. Either a
                native Python dict or the returned object from this
                package's `NetSuite.Common.TransactionSearch` (or
                similar) is acceptable.
        Returns:
            List of `zeep.xsd.ComplexType`.
        """

        def extract_records(response):
            record_list = response['body']['searchResult']['recordList']
            return record_list['record'] if record_list else []

        if not searchRecord:
            raise ValueError('Missing `searchRecord`')

        logger.info('Fetching search results, this may take a few moments ...')

        response = self.request(
            'search',
            searchRecord=searchRecord,
        )

        searchResult = response['body']['searchResult']
        if not searchResult['status']['isSuccess']:
            for error in searchResult['status']['statusDetail']:
                raise RuntimeError(f'{error["code"]}: {error["message"]}')

        records = extract_records(response)

        pageIndex = 1
        searchId = searchResult['searchId']
        pageSize = searchResult['pageSize'] or 0
        totalRecords = searchResult['totalRecords']
        totalPages = searchResult['totalPages']

        if returnAll:
            while pageIndex < totalPages:
                pageIndex += 1
                logger.info(f'Fetching search results, page {pageIndex} of {totalPages}')
                response = self.searchMoreWithId(
                    searchId=searchId,
                    pageIndex=pageIndex,
                )
                records.extend(extract_records(response))
        elif totalRecords > pageSize:
            logger.info(f'Fetched {pageSize} records and skipped remaining {totalRecords - pageSize} records.')
            logger.info('Use `returnAll=True` for return all records.')

        return records

    def searchMoreWithId(
        self,
        searchId: str,
        pageIndex: int = 1,
    ) -> None:
        """
        Paginate through remaining search results using a `searchId` str.

        Args:
            searchId: Unique ID provided by NetSuite to retrieve a
                previously performed search. Search IDs expire if they
                have not been used within 15 minutes of their creation.
            pageIndex: An index that specifies which page in the search
                to return.
        Returns:
            Search response returned by NetSuite. Record list can be
            found nested at response['body']['searchResult']['recordList']
        """

        if not searchId:
            raise ValueError('Missing `searchId`')

        return self.request(
            'searchMoreWithId',
            searchId=searchId,
            pageIndex=pageIndex,
        )

    @WebServiceCall(
        'body.readResponseList.readResponse',
        extract=lambda resp: [
            r['record'] for r in resp if r['status']['isSuccess']
        ]
    )
    def getList(
        self,
        recordType: str,
        *,
        internalIds: Sequence[int] = (),
        externalIds: Sequence[str] = ()
    ) -> List[CompoundValue]:
        """Get a list of records"""

        if len(list(internalIds) + list(externalIds)) == 0:
            raise ValueError('Please specify `internalId` and/or `externalId`')

        return self.request(
            'getList',
            self.Messages.GetListRequest(
                baseRef=[
                    self.Core.RecordRef(
                        type=recordType,
                        internalId=internalId,
                    ) for internalId in internalIds
                ] + [
                    self.Core.RecordRef(
                        type=recordType,
                        externalId=externalId,
                    ) for externalId in externalIds
                ],
            )
        )

    @WebServiceCall(
        'body.readResponse',
        extract=lambda resp:
            resp['record'] if resp['status']['isSuccess'] else resp['status']['statusDetail'],
    )
    def get(
        self,
        recordType: str,
        *,
        internalId: int = None,
        externalId: str = None
    ) -> CompoundValue:
        """Get a single record"""
        if len([v for v in (internalId, externalId) if v is not None]) != 1:
            raise ValueError('Specify either `internalId` or `externalId`')

        if internalId:
            record_ref = self.Core.RecordRef(
                type=recordType,
                internalId=internalId,
            )
        else:
            self.Core.RecordRef(
                type=recordType,
                externalId=externalId,
            )

        return self.request('get', baseRef=record_ref)

    @WebServiceCall(
        'body.getAllResult',
        extract=lambda resp: resp['recordList']['record'] if resp['status']['isSuccess'] else resp['status']['statusDetail']
    )
    def getAll(self, recordType: str) -> List[CompoundValue]:
        """Get all records of a given type."""
        return self.request(
            'getAll',
            record=self.Core.GetAllRecord(
                recordType=recordType,
            ),
        )

    @WebServiceCall(
        'body.writeResponse',
        extract=lambda resp:
            resp['baseRef'] if resp['status']['isSuccess'] else resp['status']['statusDetail'],
    )
    def add(self, record: Dict) -> CompoundValue:
        """Insert a single record."""
        return self.request('add', record=record)

    @WebServiceCall(
        'body.writeResponse',
        extract=lambda resp:
            resp['baseRef'] if resp['status']['isSuccess'] else resp['status']['statusDetail'],
    )
    def upsert(self, record: Dict) -> CompoundValue:
        """Upsert a single record."""
        return self.request('upsert', record=record)

    @WebServiceCall(
        'body.writeResponseList',
        extract=lambda resp:
            [record['baseRef'] if record['status']['isSuccess'] else record['status']['statusDetail'] for record in resp],
    )
    def upsertList(self, records: List[Dict]) -> List[CompoundValue]:
        """Upsert a list of records."""
        return self.request('upsertList', record=records)

    @WebServiceCall(
        'body.getItemAvailabilityResult.itemAvailabilityList.itemAvailability',
        default=[]
    )
    def getItemAvailability(
        self,
        *,
        internalIds: Sequence[int] = (),
        externalIds: Sequence[str] = (),
        lastQtyAvailableChange: datetime = None
    ) -> List[Dict]:
        if len(list(internalIds) + list(externalIds)) == 0:
            raise ValueError('Please specify `internalId` and/or `externalId`')

        item_filters = [
            {'type': 'inventoryItem', 'internalId': internalId}
            for internalId in internalIds
        ] + [
            {'type': 'inventoryItem', 'externalId': externalId}
            for externalId in externalIds
        ]

        return self.request(
            'getItemAvailability',
            itemAvailabilityFilter=[{
                'item': {'recordRef': item_filters},
                'lastQtyAvailableChange': lastQtyAvailableChange
            }],
        )
