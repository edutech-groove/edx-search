""" handle requests for courseware search http requests """
# This contains just the url entry points to use if desired, which currently has only one
# pylint: disable=too-few-public-methods
import logging
import json
import copy

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


def _process_field_values(request):
    """ Create separate dictionary of supported filter values provided """
    return {
        field_key: request.POST[field_key]
        for field_key in request.POST
        if field_key in course_discovery_filter_fields()
    }


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
        field_dictionary = _process_field_values(request)

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


def _get_program_facets(request):
    selected_facets = []
    status = request.POST.get('status', False)
    program_type = request.POST.get('program_type', False)
    if status and status != "All":
        selected_facets.append("status_exact:%s" % status)
    if program_type and program_type != "All":
        selected_facets.append("type_exact:%s" % program_type)
    return selected_facets


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
        "count": "",
    }
    facet_template = {
        "status": {
            "terms": {
                "active": 0,
            }
        },
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
        if page == 0: 
            page = 1
        else:
            page = page + 1
        catalog_integration = CatalogIntegration.current()
        username = catalog_integration.service_username
        user = User.objects.get(username=username)
        api = create_catalog_api_client(user, site=None)
        querystring = {
            "page": page,
            "page_size": size,
            "selected_facets": selected_facets,
        }
        response = get_edx_api_data(
            catalog_integration, 'search', api=api, resource_id="programs/facets",
            querystring=querystring, traverse_pagination=False
        )
        count = response['objects']['count']
        programs = response['objects']['results']
        fields = response['fields']
        data = {
            "results": [],
            "facets": {},
        }
        for program in programs:
            record = copy.deepcopy(dict(program))
            temp = copy.deepcopy(result_templates)
            if record['status'] == 'active':
                temp['course'] = record['title']
                if record['card_image_url'] != "None":
                    temp['image_url'] = record['card_image_url']
                if record['authoring_organizations']:
                    temp['org'] = record['authoring_organizations'][0]['name']
                temp['content']['display_name'] = record['title']
                temp['id'] = record['uuid']
                temp['programtype'] = record['type']
                temp['count'] = count
                data['results'].append(temp)
        status_data = dict()
        for status in fields['status']:
            status_data.update({status['text']: status['count'],})
        type_data = dict()
        for type in fields['type']:
            type_data.update({type['text']: type['count'],})
        facet_template['status']['terms'] = status_data
        facet_template['program_type']['terms'] = type_data
        data['facets'] = facet_template
    except User.DoesNotExist:
        logger.exception(
            'Failed to create API client. Service user {username} does not exist.'.format(username=username)
        )
    return HttpResponse(
        json.dumps(data, cls=DjangoJSONEncoder),
        content_type='application/json',
        status=200
    )
