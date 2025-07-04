import functools
from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI
from django.contrib.sessions.models import Session
from django.contrib.auth import get_user_model
from ninja.errors import HttpError, logger
from .models import AdoptedArea, Team
from .schemas import AdoptAreaInput, AdoptAreaLayer, TeamCreate, TeamOut
from typing import List

User = get_user_model()

api = NinjaAPI(
    csrf=False,
    title="Seaside Sustainability WebGIS API",
    description="Endpoints for user authentication, registration, and password management."
)


def require_auth(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        session_token = request.headers.get("X-Session-Token")
        user = get_user_from_token(session_token)
        if not user:
            return JsonResponse({"success": False, "message": "Not authenticated"}, status=401)
        request.user = user
        return view_func(request, *args, **kwargs)

    return wrapper


def require_team_leader(user, team):
    if team.leader != user:
        raise PermissionDenied("You are not the leader of this team.")


# -------------------- Helper: Resolve user from session token --------------------
def get_user_from_token(token):
    try:
        print(f"Looking for session with key: {token}")
        session = Session.objects.get(session_key=token)
        print(f"Session found: {session}")

        session_data = session.get_decoded()
        print(f"Session data: {session_data}")

        user_id = session_data.get('_auth_user_id')
        print(f"User ID from session: {user_id}")

        if not user_id:
            print("No user ID in session")
            return None

        user = User.objects.get(id=user_id)
        print(f"User found: {user}")
        return user
    except Session.DoesNotExist:
        print("Session does not exist")
        return None
    except User.DoesNotExist:
        print("User does not exist")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None


# -------------------- ADOPT AREA --------------------
@api.post("/adopt-area/", tags=["Adopt Area"])
@require_auth
def adopt_area(request, data: AdoptAreaInput):
    try:
        area_data = data.model_dump()
        location_data = area_data.get("location")
        if not isinstance(location_data, dict) or "coordinates" not in location_data:
            return JsonResponse({"success": False, "message": "Invalid location format"}, status=400)

        coordinates = location_data["coordinates"]
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
            return JsonResponse({"success": False, "message": "Coordinates must be [lng, lat]"}, status=400)

        # Convert to GEOS Point
        try:
            lng = float(coordinates[0])
            lat = float(coordinates[1])
            area_data["location"] = Point(lng, lat)
        except ValueError:
            return JsonResponse({"success": False, "message": "Coordinates must be valid numbers"}, status=400)

        area_data["user"] = request.user

        # Save to DB
        AdoptedArea.objects.create(**area_data)

        return JsonResponse({"success": True, "message": "Area adopted successfully!"}, status=201)

    except Exception as e:
        return JsonResponse({"success": False, "message": f"Failed to save area: {str(e)}"}, status=500)


@api.get("/adopted-area-layer/", response=List[AdoptAreaLayer], tags=["Adopt Area"])
def list_adopted_areas(request):
    try:
        return [
            AdoptAreaLayer(
                id=area.id,
                area_name=area.area_name,
                adoptee_name=area.adoptee_name,
                email=area.email,
                location={
                    "type": "Point",
                    "coordinates": [area.location.x, area.location.y]
                },
                city=area.city,
                state=area.state,
                country=area.country,
                note=area.note
            )
            for area in AdoptedArea.objects.filter(is_active=True)
        ]
    except Exception as e:
        return JsonResponse(
            {"success": False, "message": f"Error fetching adopted areas: {str(e)}"},
            status=500
        )


@api.put("/adopt-area/{area_id}/", tags=["Adopt Area"])
@require_auth
def update_adopted_area(request, area_id: int, data: AdoptAreaInput):
    try:
        area = AdoptedArea.objects.get(id=area_id, user=request.user)
    except AdoptedArea.DoesNotExist:
        return JsonResponse({"success": False, "message": "Adopted area not found"}, status=404)

    for field, value in data.model_dump().items():
        setattr(area, field, value)
    area.save()

    return JsonResponse({"success": True, "message": "Adopted area updated successfully!"})


@api.delete("/adopt-area/{area_id}/", tags=["Adopt Area"])
@require_auth
def delete_adopted_area(request, area_id: int):
    try:
        area = AdoptedArea.objects.get(id=area_id, user=request.user)
        area.delete()
        return JsonResponse({"success": True, "message": "Adopted area deleted successfully!"})
    except AdoptedArea.DoesNotExist:
        return JsonResponse({"success": False, "message": "Adopted area not found"}, status=404)

    # -------------------- TEAMS --------------------


@api.get("/teams/", response=List[TeamOut], tags=["Teams"])
def list_teams(request):
    teams = Team.objects.all()
    return [TeamOut.from_team(team) for team in teams]


@api.get("/teams/{team_id}/", response=TeamOut, tags=["Teams"])
def get_team(team_id: int):
    team = get_object_or_404(Team, id=team_id)
    return team


@api.put("/teams/{team_id}/", response=TeamOut, tags=["Teams"])
@require_auth
def update_team(request, team_id: int, payload: TeamCreate):
    team = get_object_or_404(Team, id=team_id)

    permission_check = require_team_leader(request.user, team)
    if permission_check:
        return permission_check  # Returns JsonResponse with 403

    team.name = payload.name
    team.description = payload.description
    team.headquarters = payload.headquarters
    team.save()
    return team


@api.delete("/teams/{team_id}/", tags=["Teams"])
@require_auth
def delete_team(request, team_id: int):
    team = get_object_or_404(Team, id=team_id)

    permission_check = require_team_leader(request.user, team)
    if permission_check:
        return permission_check

    team.delete()
    return JsonResponse({"success": True, "message": "Team deleted successfully"})


@api.post("/teams/", response=TeamOut, tags=["Teams"])
@require_auth
def create_team(request, payload: TeamCreate):
    try:
        lng, lat = payload.headquarters["coordinates"]

        team = Team.objects.create(
            name=payload.name,
            description=payload.description,
            headquarters=Point(lng, lat),
            city=payload.city or "",
            state=payload.state or "",
            country=payload.country,
        )
        team.leaders.add(request.user)
        team.members.add(request.user)

        return TeamOut.from_team(team)

    except Exception as e:
        import traceback
        logger.error("🔥 create_team error: %s", traceback.format_exc())
        raise HttpError(500, f"Failed to create team: {str(e)}")


@api.post("/teams/{team_id}/join", tags=["Teams"])
@require_auth
def join_team(request, team_id: int):
    team = get_object_or_404(Team, id=team_id)
    team.members.add(request.user)
    return {"success": True}


@api.post("/teams/{team_id}/leave", tags=["Teams"])
@require_auth
def leave_team(request, team_id: int):
    team = get_object_or_404(Team, id=team_id)
    team.members.remove(request.user)
    team.leaders.remove(request.user)
    return {"success": True}
