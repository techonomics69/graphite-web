from django.conf.urls import url
from . import views

urlpatterns = [
    url('^save/(?P<name>[^/]+)', views.save, name='dashboard_save'),
    url('^save_template/(?P<name>[^/]+)/(?P<key>[^/]+)',
        views.save_template, name='dashboard_save_template'),
    url('^load/(?P<name>[^/]+)', views.load, name='dashboard_load'),
    url('^load/(?P<name>[^/]+)/(?P<val>[^/]+)', views.load_template,
        name='dashboard_load_template'),
    url('^load_template/(?P<name>[^/]+)/(?P<val>[^/]+)', views.load_template,
        name='dashboard_load_template'),
    url('^delete/(?P<name>[^/]+)', views.delete, name='dashboard_delete'),
    url('^create-temporary/?', views.create_temporary,
        name='dashboard_create_temporary'),
    url('^email', views.email, name='dashboard_email'),
    url('^find/', views.find, name='dashboard_find'),
    url('^delete_template/(?P<name>[^/]+)', views.delete_template,
        name='dashboard_delete_template'),
    url('^find_template/', views.find_template,
        name='dashboard_find_template'),
    url('^login/?', views.user_login, name='dashboard_login'),
    url('^logout/?', views.user_logout, name='dashboard_logout'),
    url('^help/', views.help, name='dashboard_help'),
    url('^(?P<name>[^/]+)/(?P<val>[^/]+)', views.template,
        name='dashboard_template'),
    url('^(?P<name>[^/]+)', views.dashboard, name='dashboard'),
    url('', views.dashboard, name='dashboard'),
]
