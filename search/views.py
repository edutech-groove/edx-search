""" handle requests for courseware search http requests """
# This contains just the url entry points to use if desired, which currently has only one
# pylint: disable=too-few-public-methods
import logging
import json
import copy

from datetime import datetime
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_POST

from eventtracking import tracker as track
from .api import QueryParseError, perform_search, course_discovery_search, course_discovery_filter_fields
from .initializer import SearchInitializer
from openedx.features.courses_programs_search import views as courses_programs_views
from openedx.core.djangoapps.catalog.utils import create_catalog_api_client
from openedx.core.djangoapps.catalog.models import CatalogIntegration
from openedx.core.lib.edx_api_utils import get_edx_api_data
from django.contrib.auth import get_user_model
from .utils import get_discovery_facet

# log appears to be standard name used for logger
log = logging.getLogger(__name__)  # pylint: disable=invalid-name
User = get_user_model()  # pylint: disable=invalid-name


def _process_pagination_values(request):
    """ process pagination requests from request parameter """
    size = 20
    page = 0
    from_ = 0
    if "page_size" in request.POST:
        size = int(request.POST["page_size"])
        max_page_size = getattr(settings, "SEARCH_MAX_PAGE_SIZE", 100)
        # The parens below are superfluous, but make it much clearer to the reader what is going on
        if not (0 < size <= max_page_size):  # pylint: disable=superfluous-parens
            raise ValueError(_('Invalid page size of {page_size}').format(page_size=size))

        if "page_index" in request.POST:
            page = int(request.POST["page_index"])
            from_ = page * size
    return size, from_, page


def _process_field_values(request): # replaced by _get_field_values()
    """ Create separate dictionary of supported filter values provided """
    return {
        field_key: request.POST[field_key]
        for field_key in request.POST
        if field_key in course_discovery_filter_fields()
    }


def _get_field_values(request):
    # DEFAULT_FILTER_FIELDS = ["org", "org[]", "modes", "modes[]", "language", "language[]"]
    dict = {}
    for field_key in request.POST:
        if field_key in course_discovery_filter_fields():
            if field_key.find('[]') != -1:
                value = request.POST.getlist(field_key, False)
                field_key = field_key.split('[]')[0]
                value_field = {field_key: value}
                dict.update(value_field)
            else:
                value = request.POST[field_key]
                value_field = {field_key: value}
                dict.update(value_field)
    return dict


# @require_POST 
def do_search(request, course_id=None):
    """
    Search view for http requests

    Args:
        request (required) - django request object
        course_id (optional) - course_id within which to restrict search

    Returns:
        http json response with the following fields
            "took" - how many seconds the operation took
            "total" - how many results were found
            "max_score" - maximum score from these results
            "results" - json array of result documents

            or

            "error" - displayable information about an error that occured on the server

    POST Params:
        "search_string" (required) - text upon which to search
        "page_size" (optional)- how many results to return per page (defaults to 20, with maximum cutoff at 100)
        "page_index" (optional) - for which page (zero-indexed) to include results (defaults to 0)
    """

    # Setup search environment
    SearchInitializer.set_search_enviroment(request=request, course_id=course_id)

    results = {
        "error": _("Nothing to search")
    }
    status_code = 500

    search_term = request.POST.get("search_string", None)

    if request.method == "GET":
        return courses_programs_views.index(request)
    else:
        try:
            if not search_term:
                raise ValueError(_('No search term provided for search'))

            size, from_, page = _process_pagination_values(request)
            # Analytics - log search request
            track.emit(
                'edx.course.search.initiated',
                {
                    "search_term": search_term,
                    "page_size": size,
                    "page_number": page,
                }
            )

            results = perform_search(
                search_term,
                user=request.user,
                size=size,
                from_=from_,
                course_id=course_id
            )

            status_code = 200

            # Analytics - log search results before sending to browser
            track.emit(
                'edx.course.search.results_displayed',
                {
                    "search_term": search_term,
                    "page_size": size,
                    "page_number": page,
                    "results_count": results["total"],
                }
            )

        except ValueError as invalid_err:
            results = {
                "error": unicode(invalid_err)
            }
            log.debug(unicode(invalid_err))

        except QueryParseError:
            results = {
                "error": _('Your query seems malformed. Check for unmatched quotes.')
            }

        # Allow for broad exceptions here - this is an entry point from external reference
        except Exception as err:  # pylint: disable=broad-except
            results = {
                "error": _('An error occurred when searching for "{search_string}"').format(search_string=search_term)
            }
            log.exception(
                'Search view exception when searching for %s for user %s: %r',
                search_term,
                request.user.id,
                err
            )

        return HttpResponse(
            json.dumps(results, cls=DjangoJSONEncoder),
            content_type='application/json',
            status=status_code
        )


@require_POST
def course_discovery(request):
    """
    Search for courses

    Args:
        request (required) - django request object

    Returns:
        http json response with the following fields
            "took" - how many seconds the operation took
            "total" - how many results were found
            "max_score" - maximum score from these resutls
            "results" - json array of result documents

            or

            "error" - displayable information about an error that occured on the server

    POST Params:
        "search_string" (optional) - text with which to search for courses
        "page_size" (optional)- how many results to return per page (defaults to 20, with maximum cutoff at 100)
        "page_index" (optional) - for which page (zero-indexed) to include results (defaults to 0)
    """
    results = {
        "error": _("Nothing to search")
    }
    status_code = 500

    search_term = request.POST.get("search_string", None)

    try:
        size, from_, page = _process_pagination_values(request)
        field_dictionary = _get_field_values(request)
       
        # Analytics - log search request
        track.emit(
            'edx.course_discovery.search.initiated',
            {
                "search_term": search_term,
                "page_size": size,
                "page_number": page,
            }
        )

        results = course_discovery_search(
            search_term=search_term,
            size=size,
            from_=from_,
            field_dictionary=field_dictionary,
        )

        # Analytics - log search results before sending to browser
        track.emit(
            'edx.course_discovery.search.results_displayed',
            {
                "search_term": search_term,
                "page_size": size,
                "page_number": page,
                "results_count": results["total"],
            }
        )

        status_code = 200

    except ValueError as invalid_err:
        results = {
            "error": unicode(invalid_err)
        }
        log.debug(unicode(invalid_err))

    except QueryParseError:
        results = {
            "error": _('Your query seems malformed. Check for unmatched quotes.')
        }

    # Allow for broad exceptions here - this is an entry point from external reference
    except Exception as err:  # pylint: disable=broad-except
        results = {
            "error": _('An error occurred when searching for "{search_string}"').format(search_string=search_term)
        }
        log.exception(
            'Search view exception when searching for %s for user %s: %r',
            search_term,
            request.user.id,
            err
        )
   
    return HttpResponse(
        json.dumps(results, cls=DjangoJSONEncoder),
        content_type='application/json',
        status=status_code
    )


def get_catalog_integration_api(request):
    catalog_integration = CatalogIntegration.current()
    username = catalog_integration.service_username
    user = User.objects.get(username=username)
    api = create_catalog_api_client(user, site=None)
    return catalog_integration, username, api


def _get_program_facets(request):
    selected_facets = []
    status = request.POST.getlist('status[]', False)
    program_type = request.POST.getlist('program_type[]', False)
    if status:
        for item in status:
            selected_facets.append("status_exact:%s" % item)
    if program_type:
        for item in program_type:
            selected_facets.append("type_exact:%s" % item)
    return selected_facets


def is_archived_courserun(courserun):
    if courserun.get('end'):
        return datetime.strptime(courserun['end'], '%Y-%m-%dT%H:%M:%SZ') < datetime.now()
    return False


@require_POST
def auto_suggestion(request):
    course_template = {
        "type": "Course",
        "records": [],
    }
    program_template = {
        "type": "Program",
        "records": [],
    }
    record = {
        "name": "",
        "org": "",
        "url": "",
    }
    try:
        catalog_integration, username, api = get_catalog_integration_api(request)
        search_term = request.POST.get("search_string", None)
        querystring = {
            "q": search_term,
        }
        response = get_edx_api_data(
            catalog_integration, 
            'search', 
            api=api, 
            resource_id="all",
            querystring=querystring,
            traverse_pagination=False
        )
        courses_items = programs_items = 0
        if response["results"]:
            for item in response["results"]:
                if (
                    item["content_type"] == "course" and 
                    courses_items <= 3 and not 
                    is_archived_courserun(item["course_runs"][0])
                ):
                    course_temp = copy.deepcopy(record)
                    course_temp["name"] = item["title"]
                    course_temp["url"] = item["course_runs"][0]["key"]
                    course_template["records"].append(course_temp)
                    courses_items += 1

                elif item["content_type"] == "program" and programs_items <= 3:
                    program_temp = copy.deepcopy(record)
                    program_temp["name"] = item["title"]
                    program_temp["url"] = item["uuid"]
                    program_template["records"].append(program_temp)
                    programs_items += 1
        data_response = [course_template, program_template]
    except User.DoesNotExist:
        log.exception(
            'Failed to create API client. Service user {username} does not exist.'.format(username=username)
        )
    return HttpResponse(
        json.dumps(data_response, cls=DjangoJSONEncoder),
        content_type='application/json',
        status=200
    )


@require_POST
def program_discovery(request):
    """
    Search Programs lists
    """
    results = {
        "error": _("Nothing to search")
    }
    result_templates = {
        "modes": [],
        "language": "en",
        "course": "",
        "number": "",
        "content": {
            "display_name": "No data",
        },
        "start": "",
        "image_url": "",
        "org": "",
        "id": "",
        "programtype": "",
        "course_count": 0,
    }
    facet_template = {
        "program_type": {
            "terms": {
                "Masters": 0,
                "Professional Certificate": 0,
            }
        }
    }

    try:
        size, from_, page = _process_pagination_values(request)
        selected_facets = _get_program_facets(request)
        page += 1
        catalog_integration, username, api = get_catalog_integration_api(request)
        search_term = request.POST.get("search_string", None)
        data = {
            "results": [],
            "facets": {},
            "total": 0,
        }
        querystring = {
            "page": page,
            "page_size": size,
            "selected_facets": selected_facets,
            "q": search_term,
        }
        response = get_edx_api_data(
            catalog_integration, 
            'search', 
            api=api, 
            resource_id="programs/facets",
            querystring=querystring,
            traverse_pagination=False
        )
        if response != []:
            count = response['objects']['count'] and response['objects']['count'] or 0
            programs = response['objects']['results'] and response['objects']['results'] or []
            fields = response['fields'] and response['fields'] or []
            for program in programs:
                record = copy.deepcopy(dict(program))
                temp = copy.deepcopy(result_templates)
                if record['status'] == 'active':
                    temp['course'] = record['title']
                    if record['card_image_url'] != "null":
                        temp['image_url'] = record['card_image_url']
                    if record['authoring_organizations']:
                        temp['org'] = record['authoring_organizations'][0]['name']
                    temp['content']['display_name'] = record['title']
                    temp['id'] = record['uuid']
                    temp['programtype'] = record['type']
                    temp['course_count'] = record['course_count']
                    data['results'].append(temp)
            if fields:
                type_data = dict()
                for type in fields['type']:
                    type_data.update({type['text']: type['count'],})
            facet_template['program_type']['terms'] = type_data
            data['total'] = count
            data['facets'] = facet_template
    except User.DoesNotExist:
        log.exception(
            'Failed to create API client. Service user {username} does not exist.'.format(username=username)
        )
    return HttpResponse(
        json.dumps(data, cls=DjangoJSONEncoder),
        content_type='application/json',
        status=200
    )


def _get_selected_filter(request, resource_id=None):
    if not resource_id:
        resource_id = request.POST.get('resource_id', 'all')
    program_types = request.POST.getlist('program_types[]', [])
    seat_types = request.POST.getlist('seat_types[]', [])
    organizations = request.POST.getlist('organizations[]', [])
    selected_filter = {
        "seat_types": seat_types,
        "program_types": program_types,
    }
    if resource_id in ['all', 'programs']:
        selected_filter.update({
            "authoring_organizations": organizations,
        })
    if resource_id == 'course_runs':
        selected_filter.update({
            "organizations": organizations,
        })
    return selected_filter


def update_facets(res_facets, facets_template):
    for key, value in facets_template.items():
        dict = {}
        data_facets = res_facets.get(key, [])
        temp_facets = facets_template.get(key)
        for line in data_facets:
            if temp_facets.get(line['text']):
                dict.update({line['text']: line['count'] + temp_facets[line['text']]})
            else:
                dict.update({line['text']: line['count']})
        facets_template[key].update(dict)
    return facets_template


def get_discovery_results(request, resource_id, querystring, catalog_integration, api):
    lst_results = []
    lst_resources = [resource_id]
    if resource_id == 'all':
        lst_resources = ['course_runs', 'programs']
    for res in lst_resources:
        if res == 'programs' and resource_id == 'all':
            querystring.update({'page_size': 4})
        selected_filter = {}
        selected_filter = _get_selected_filter(request, res)
        selected_filter.update(querystring)
        source = {}
        response = get_edx_api_data(
            catalog_integration, 
            'search', 
            api=api, 
            resource_id=res + '/facets',
            querystring=selected_filter,
            traverse_pagination=False
        )
        if response != []:
            source[res] = response
            lst_results.append(source)

    return lst_results


def rewrite_org(lst_org):
    return [ org.split(':')[1].strip() for org in lst_org ]
   

@require_POST
def discovery(request):
    """
    Discovery Course and Program

    """
    RESULT_TEMPLATE = {
        "content_type": "",
        "title": "",
        "program_types": "",
        "id": "",
        "image_url": "",
        "org": [],
        "course_count": 0,
        "start": "",
        "number": "",
    }
    FACET_TEMPLATE = copy.deepcopy(dict(get_discovery_facet()))
    DATA_RESPONSE = {
        "facets": FACET_TEMPLATE,
        "program_results": [],
        "course_results": [],
        "course_count": 0,
        "program_count": 0,
    }
    try:
        size, from_, page = _process_pagination_values(request)
        page += 1
        catalog_integration, username, api = get_catalog_integration_api(request)
        search_term = request.POST.get("search_string", None)
        resource_id = request.POST.get('resource_id', 'all')
        if resource_id not in ['all', 'programs', 'course_runs']:
             return HttpResponse({}, status=404)
        querystring = {
            "page": page,
            "page_size": size,
            "q": search_term,
            "is_archived": "false",
        }
        lst_results = get_discovery_results(request, resource_id, querystring, catalog_integration, api)
        for res in lst_results:
            resource_id = list(res.keys())[0]
            results = res[resource_id]['objects']['results']
            count = res[resource_id]['objects']['count']
            for result in results:
                record = copy.deepcopy(dict(result))
                temp = copy.deepcopy(RESULT_TEMPLATE)
                temp['title'] = record['title']
                temp['content_type'] = record['content_type']
                if record['content_type'] == 'program':
                    temp['id'] = record['uuid']
                    temp['image_url'] = record['card_image_url']
                    temp['course_count'] = record['course_count']
                    temp['program_types'] = record['program_types']
                    if record['authoring_organizations']:
                        temp['org'] = [org['name'] for org in record['authoring_organizations']]
                if record['content_type'] == 'courserun':
                    temp['id'] = record['key']
                    temp['image_url'] = record['image_url']
                    temp['org'] = rewrite_org(record['organizations'])
                    temp['start'] = record['start']
                    temp['number'] = record['number']
                if resource_id == 'course_runs':
                    DATA_RESPONSE['course_results'].append(temp)
                    DATA_RESPONSE['course_count'] = count
                if resource_id == 'programs':
                    DATA_RESPONSE['program_results'].append(temp)
                    DATA_RESPONSE['program_count'] = count
            DATA_RESPONSE['facets'] = update_facets(res[resource_id]['fields'], FACET_TEMPLATE)

    except User.DoesNotExist:
        log.exception(
            'Failed to create API client. Service user {username} does not exist.'.format(username=username)
        )
    return HttpResponse(
        json.dumps(DATA_RESPONSE, cls=DjangoJSONEncoder),
        content_type='application/json',
        status=200
    )


def facets(request):
    FACET_TEMPLATE = copy.deepcopy(dict(get_discovery_facet()))
    try:
        catalog_integration, username, api = get_catalog_integration_api(request)
        response = get_edx_api_data(
            catalog_integration, 
            'search', 
            api=api,
            resource_id="all/facets",
            querystring={
                "is_archived": "false",
            },
            traverse_pagination=False
        )
        if response != []:
            update_facets(response['fields'], FACET_TEMPLATE)
    except User.DoesNotExist:
        log.exception(
            'Failed to create API client. Service user {username} does not exist.'.format(username=username)
        )
    return HttpResponse(
        json.dumps(FACET_TEMPLATE, cls=DjangoJSONEncoder),
        content_type='application/json',
        status=200
    )
